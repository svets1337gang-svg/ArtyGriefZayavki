"""Discord UI: панель подачи, выбор режима, кнопки модерации.

Все компоненты, которые должны пережить перезапуск, сделаны persistent:
  * кнопка панели — статический custom_id + bot.add_view();
  * кнопки модерации — discord.ui.DynamicItem с custom_id вида
    "application_approve:<id>", восстанавливаются через bot.add_dynamic_items().

Состояние заявки никогда не хранится в памяти процесса — только в SQLite.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import discord

import config
import database as db

log = logging.getLogger(__name__)

COLOR_PENDING = discord.Color.blurple()
COLOR_APPROVED = discord.Color.green()
COLOR_REJECTED = discord.Color.red()

PANEL_BUTTON_CUSTOM_ID = "application_panel:open"
PANEL_DOCUMENTS_BUTTON_CUSTOM_ID = "application_panel:documents_info"
PANEL_MESSAGE_SETTING_KEY = "panel_message_id"
PANEL_CHANNEL_SETTING_KEY = "panel_channel_id"
RECRUITMENT_STATUS_SETTING_KEY = "recruitment_open"


# --------------------------------------------------------------------------- #
# Вспомогательные функции
# --------------------------------------------------------------------------- #

async def safe_respond(interaction: discord.Interaction, content: str) -> None:
    """Отправляет ephemeral-ответ, не падая на истёкшем/использованном interaction."""
    try:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
        else:
            await interaction.response.send_message(content, ephemeral=True)
    except (discord.HTTPException, discord.NotFound) as exc:
        log.warning("Не удалось ответить на interaction: %s", exc)


def get_blacklist_blocked_role(member: discord.Member) -> Optional[discord.Role]:
    """Возвращает первую роль участника из BLACKLIST_ROLE_IDS или None."""
    if not config.BLACKLIST_ROLE_IDS:
        return None
    blacklist = set(config.BLACKLIST_ROLE_IDS)
    for role in member.roles:
        if role.id in blacklist:
            return role
    return None


async def check_eligibility(member: discord.Member) -> Tuple[bool, Optional[str]]:
    """Можно ли участнику подать новую заявку.

    Порядок проверок: чёрный список ролей -> Pending -> активный cooldown ->
    (опц.) Approved.

    Одобренная заявка по умолчанию НЕ блокирует новую подачу: пользователь может
    подать заявку повторно (например, на второй режим или после ухода из команды).
    Поведение переключается флагом config.BLOCK_AFTER_APPROVAL.
    """
    user_id = member.id

    blocked_role = (
        get_blacklist_blocked_role(member)
        if isinstance(member, discord.Member)
        else None
    )
    if blocked_role is not None:
        # Название роли пользователю не сообщаем намеренно; детали — в логах.
        log.info(
            "Подача заявки заблокирована: пользователь %s имеет роль %s (id=%s) из чёрного списка.",
            user_id,
            blocked_role.name,
            blocked_role.id,
        )
        return False, "❌ Вы не можете подать заявку."

    pending = await db.get_pending_application(user_id)
    if pending is not None:
        return False, "❌ У вас уже есть заявка на рассмотрении."

    cooldown = await db.get_active_cooldown(user_id)
    if cooldown is not None:
        expires_at = int(cooldown["expires_at"])
        return False, (
            "❌ Ваша заявка была отклонена. "
            f"Подать новую заявку можно <t:{expires_at}:R> (<t:{expires_at}:f>)."
        )

    if config.BLOCK_AFTER_APPROVAL:
        last = await db.get_last_application(user_id)
        if last is not None and last["status"] == "Approved":
            return False, "❌ Ваша заявка уже одобрена — повторная подача не требуется."

    return True, None


def _clip(value: str, limit: int = 1024) -> str:
    """Обрезает значение под лимит поля Embed (после экранирования Markdown)."""
    value = value or "—"
    if len(value) > limit:
        return value[: limit - 1].rstrip() + "…"
    return value


def build_decision_dm_embed(*, approved: bool, cooldown_expires: Optional[int] = None) -> discord.Embed:
    """Создаёт Embed для ЛС пользователю о решении по заявке."""
    if approved:
        embed = discord.Embed(
            title="✅ Заявка одобрена",
            description=(
                "Ваша заявка в команду проекта была одобрена. Вам выдана роль «Кандидат». "
                "Для дальнейшего прохождения отбора необходимо записаться на обзвон "
                "в соответствующем канале."
            ),
            color=COLOR_APPROVED,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
    else:
        description = (
            "К сожалению, ваша заявка в команду проекта была **отклонена**. "
            "Вы сможете подать заявку повторно через **7 дней**."
        )
        if cooldown_expires:
            description += f"\n\nТочное время: <t:{cooldown_expires}:f> (<t:{cooldown_expires}:R>)"

        embed = discord.Embed(
            title="❌ Заявка отклонена",
            description=description,
            color=COLOR_REJECTED,
            timestamp=dt.datetime.now(dt.timezone.utc),
        )
    return embed


def build_documents_info_embed() -> discord.Embed:
    """Embed для кнопки «Какие документы нужны» на панели подачи."""
    return discord.Embed(
        title="📄 Какие документы нужны",
        description=config.DOCUMENTS_INFO_TEXT,
        color=COLOR_PENDING,
    )


def build_documents_request_dm_embed(moderator: discord.abc.User) -> discord.Embed:
    """Embed для ЛС пользователю после нажатия модератором «Запросить документы»."""
    embed = discord.Embed(
        title=config.DOCUMENTS_DM_TITLE,
        description=config.DOCUMENTS_DM_TEXT,
        color=COLOR_PENDING,
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    embed.add_field(name="Скинуть", value=moderator.mention, inline=False)
    return embed


def _application_jump_url(mode: str, message_id: Optional[int]) -> Optional[str]:
    """Ссылка на сообщение модерации. None, если message_id ещё не сохранён."""
    if not message_id:
        return None
    channel_id = (
        config.RW_APPLICATION_CHANNEL_ID
        if mode == "RW"
        else config.FT_APPLICATION_CHANNEL_ID
    )
    return f"https://discord.com/channels/{config.GUILD_ID}/{channel_id}/{message_id}"


def build_stats_embed(stats: Dict[str, Any]) -> discord.Embed:
    by_status = stats["by_status"]
    by_mode = stats["by_mode"]
    embed = discord.Embed(
        title="📊 Статистика заявок",
        color=COLOR_PENDING,
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    embed.add_field(name="Всего заявок", value=str(stats["total"]), inline=False)
    embed.add_field(
        name="По статусам",
        value=(
            f"⏳ На рассмотрении: {by_status['Pending']}\n"
            f"✅ Одобрено: {by_status['Approved']}\n"
            f"❌ Отклонено: {by_status['Rejected']}"
        ),
        inline=True,
    )
    embed.add_field(
        name="По режимам",
        value=f"RW: {by_mode['RW']}\nFT: {by_mode['FT']}",
        inline=True,
    )
    embed.add_field(
        name="📄 Документы",
        value=f"Запрошены: {stats.get('documents_requested', 0)}",
        inline=True,
    )
    return embed


_PENDING_DESC_BUDGET = 4000  # запас под лимит описания Embed (4096)


def build_pending_embed(pending: List[Dict[str, Any]]) -> discord.Embed:
    total = len(pending)
    lines: List[str] = []
    used = 0
    shown = 0
    for app in pending:
        app_id = int(app["id"])
        user_id = int(app["user_id"])
        mode = app["mode"]
        timestamp = int(app["timestamp"])
        url = _application_jump_url(mode, app.get("message_id"))
        link = f"[открыть]({url})" if url else "—"
        line = f"**#{app_id}** · <@{user_id}> · `{mode}` · <t:{timestamp}:F> · {link}"
        if used + len(line) + 1 > _PENDING_DESC_BUDGET:
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1

    description = "\n".join(lines)
    if shown < total:
        description += f"\n\n…и ещё {total - shown} (показаны первые {shown} из {total})."

    embed = discord.Embed(
        title="⏳ Заявки на рассмотрении",
        description=description,
        color=COLOR_PENDING,
        timestamp=dt.datetime.now(dt.timezone.utc),
    )
    embed.set_footer(text=f"Всего на рассмотрении: {total}")
    return embed


def build_application_embed(
    *,
    application_id: int,
    user: discord.abc.User,
    mode: str,
    nickname: str,
    age: int,
    timezone_text: str,
    blacklist_text: str,
    motivation_text: str,
    created_at: dt.datetime,
) -> discord.Embed:
    """Embed заявки для канала модерации."""
    embed = discord.Embed(
        title=f"Заявка #{application_id} · режим {mode}",
        color=COLOR_PENDING,
        timestamp=created_at,
    )
    embed.set_author(name=str(user), icon_url=user.display_avatar.url)
    embed.add_field(name="Пользователь", value=f"{user.mention}\n`{user}`", inline=True)
    embed.add_field(name="Discord ID", value=f"`{user.id}`", inline=True)
    embed.add_field(name="Режим", value=mode, inline=True)
    embed.add_field(name="Ник", value=_clip(discord.utils.escape_markdown(nickname)), inline=True)
    embed.add_field(name="Возраст", value=str(age), inline=True)
    embed.add_field(
        name="Часовой пояс",
        value=_clip(discord.utils.escape_markdown(timezone_text)),
        inline=True,
    )
    embed.add_field(
        name="ЧСП/ЧСС",
        value=_clip(discord.utils.escape_markdown(blacklist_text)),
        inline=False,
    )
    embed.add_field(
        name="Почему мы должны взять именно вас?",
        value=_clip(discord.utils.escape_markdown(motivation_text)),
        inline=False,
    )
    embed.add_field(
        name="Дата подачи",
        value=f"<t:{int(created_at.timestamp())}:F>",
        inline=False,
    )
    embed.set_footer(text=f"Статус: Pending · ID заявки: {application_id}")
    return embed


async def send_dm(
    bot: discord.Client,
    user_id: int,
    content: str = "",
    *,
    embed: Optional[discord.Embed] = None,
) -> bool:
    """Пытается отправить ЛС. Возвращает False, если ЛС закрыты/пользователь удалён."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    except (discord.NotFound, discord.HTTPException) as exc:
        log.warning("Не удалось получить пользователя %s для ЛС: %s", user_id, exc)
        return False

    try:
        await user.send(content=content or None, embed=embed)
        return True
    except discord.Forbidden:
        log.warning("ЛС пользователя %s закрыты — сообщение не доставлено.", user_id)
        return False
    except discord.HTTPException as exc:
        log.warning("Ошибка Discord API при отправке ЛС пользователю %s: %s", user_id, exc)
        return False


# --------------------------------------------------------------------------- #
# Панель подачи заявки (persistent)
# --------------------------------------------------------------------------- #
class ApplicationPanelView(discord.ui.View):
    """Кнопка под панелью. timeout=None + постоянный custom_id."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Подать заявку",
        emoji="📋",
        style=discord.ButtonStyle.primary,
        custom_id=PANEL_BUTTON_CUSTOM_ID,
    )
    async def open_application(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.guild is None:
            await safe_respond(interaction, "❌ Подать заявку можно только на сервере.")
            return

        # Проверка статуса набора
        try:
            is_open = await db.get_recruitment_status()
        except Exception:
            log.exception("Ошибка БД при проверке статуса набора.")
            await safe_respond(interaction, "❌ Внутренняя ошибка. Попробуйте позже.")
            return

        if not is_open:
            await safe_respond(interaction, "❌ **Набор закрыт!**")
            return

        try:
            allowed, reason = await check_eligibility(interaction.user)
        except Exception:
            log.exception("Ошибка БД при проверке права на подачу заявки.")
            await safe_respond(interaction, "❌ Внутренняя ошибка. Попробуйте позже.")
            return

        if not allowed:
            await safe_respond(interaction, reason or "❌ Сейчас подать заявку нельзя.")
            return

        await interaction.response.send_message(
            "Выберите режим, на который хотите подать заявку:",
            view=ModeSelectView(interaction),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Какие документы нужны",
        emoji="📄",
        style=discord.ButtonStyle.secondary,
        custom_id=PANEL_DOCUMENTS_BUTTON_CUSTOM_ID,
    )
    async def show_documents_info(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.send_message(
            embed=build_documents_info_embed(),
            ephemeral=True,
        )


class ModeSelectView(discord.ui.View):
    """Ephemeral-сообщение с Select Menu. Живёт только в рамках одной сессии."""

    def __init__(self, origin_interaction: discord.Interaction) -> None:
        super().__init__(timeout=180)
        self.origin_interaction = origin_interaction
        self.owner_id = origin_interaction.user.id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Сообщение ephemeral, но проверку владельца делаем явно на сервере.
        if interaction.user.id != self.owner_id:
            await safe_respond(interaction, "❌ Это меню открыто другим пользователем.")
            return False
        return True

    async def on_timeout(self) -> None:
        try:
            await self.origin_interaction.edit_original_response(
                content="⌛ Время выбора истекло. Нажмите кнопку подачи заявки ещё раз.",
                view=None,
            )
        except (discord.HTTPException, discord.NotFound):
            pass

    @discord.ui.select(
        placeholder="Режим заявки",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(label="ReallyWorld", value="RW", description="Заявка на режим ReallyWorld"),
            discord.SelectOption(label="FunTime", value="FT", description="Заявка на режим FunTime"),
        ],
    )
    async def select_mode(
        self,
        interaction: discord.Interaction,
        select: discord.ui.Select,
    ) -> None:
        mode = select.values[0]
        if mode not in config.MODES:
            await safe_respond(interaction, "❌ Неизвестный режим.")
            return

        # Импорт внутри функции: modals.py импортирует views.py (ModerationView,
        # build_application_embed), поэтому обратный импорт делаем «ленивым».
        from modals import ApplicationModal

        self.stop()
        await interaction.response.send_modal(
            ApplicationModal(mode=mode, origin_interaction=self.origin_interaction)
        )


# --------------------------------------------------------------------------- #
# Кнопки модерации (persistent, состояние — в custom_id и SQLite)
# --------------------------------------------------------------------------- #
class _DecisionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"application_decision_base:(?P<app_id>[0-9]+)",
):
    """Базовый класс кнопки решения. Наследники задают template и параметры."""

    approve: bool = True

    def __init__(self, application_id: int, button: discord.ui.Button) -> None:
        self.application_id = application_id
        super().__init__(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            await safe_respond(interaction, "❌ Действие доступно только на сервере.")
            return False
        return True

    async def callback(self, interaction: discord.Interaction) -> None:
        await process_decision(interaction, self.application_id, approve=self.approve)


class ApproveButton(_DecisionButton, template=r"application_approve:(?P<app_id>[0-9]+)"):
    approve = True

    def __init__(self, application_id: int) -> None:
        super().__init__(
            application_id,
            discord.ui.Button(
                label="Одобрить",
                emoji="🟢",
                style=discord.ButtonStyle.success,
                custom_id=f"application_approve:{application_id}",
            ),
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: "re.Match[str]",
    ) -> "ApproveButton":
        return cls(int(match["app_id"]))


class RejectButton(_DecisionButton, template=r"application_reject:(?P<app_id>[0-9]+)"):
    approve = False

    def __init__(self, application_id: int) -> None:
        super().__init__(
            application_id,
            discord.ui.Button(
                label="Отказать",
                emoji="🔴",
                style=discord.ButtonStyle.danger,
                custom_id=f"application_reject:{application_id}",
            ),
        )

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: "re.Match[str]",
    ) -> "RejectButton":
        return cls(int(match["app_id"]))


class DocumentsRequestButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"application_request_documents:(?P<app_id>[0-9]+)",
):
    """Persistent-кнопка запроса документов у кандидата.

    Работает после перезапуска: id заявки лежит в custom_id, состояние запроса —
    в SQLite (documents_requested_at).
    """

    def __init__(self, application_id: int) -> None:
        super().__init__(
            discord.ui.Button(
                label="Запросить документы",
                emoji="📄",
                style=discord.ButtonStyle.secondary,
                custom_id=f"application_request_documents:{application_id}",
            )
        )
        self.application_id = application_id

    @classmethod
    async def from_custom_id(
        cls,
        interaction: discord.Interaction,
        item: discord.ui.Button,
        match: "re.Match[str]",
    ) -> "DocumentsRequestButton":
        return cls(int(match["app_id"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not isinstance(interaction.user, discord.Member):
            await safe_respond(interaction, "❌ Действие доступно только на сервере.")
            return False
        return True

    async def callback(self, interaction: discord.Interaction) -> None:
        await process_documents_request(interaction, self.application_id)


class ModerationView(discord.ui.View):
    """View с кнопками решения. Нужен только для первичной отправки сообщения:
    после перезапуска кнопки восстанавливаются через bot.add_dynamic_items()."""

    def __init__(self, application_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(ApproveButton(application_id))
        self.add_item(RejectButton(application_id))
        self.add_item(DocumentsRequestButton(application_id))


# --------------------------------------------------------------------------- #
# Обработка запроса документов
# --------------------------------------------------------------------------- #
async def process_documents_request(
    interaction: discord.Interaction,
    application_id: int,
) -> None:
    """Шлёт кандидату в ЛС embed с запросом паспорта; контакт — нажавший модератор."""
    try:
        await interaction.response.defer(ephemeral=True)
    except (discord.HTTPException, discord.NotFound) as exc:
        log.warning("Interaction устарел: %s", exc)
        return

    try:
        application = await db.get_application(application_id)
    except Exception:
        log.exception("Ошибка БД при чтении заявки %s.", application_id)
        await safe_respond(interaction, "❌ Внутренняя ошибка базы данных.")
        return

    if application is None:
        await safe_respond(interaction, "❌ Заявка не найдена в базе данных (устаревшее сообщение).")
        return

    user_id = int(application["user_id"])

    # Атомарный «захват»: повторное нажатие кнопки вторым модератором не удвоит ЛС.
    try:
        marked = await db.mark_documents_requested(application_id, interaction.user.id)
    except Exception:
        log.exception("Ошибка БД при отметке запроса документов для заявки %s.", application_id)
        await safe_respond(interaction, "❌ Внутренняя ошибка базы данных.")
        return

    if not marked:
        await safe_respond(interaction, "ℹ️ Документы по этой заявке уже были запрошены.")
        return

    dm_embed = build_documents_request_dm_embed(interaction.user)
    dm_ok = await send_dm(interaction.client, user_id, embed=dm_embed)

    # Если ЛС не доставлено, откатываем отметку — иначе запрос «залипнет»
    # в базе и модератор не сможет повторить его после решения проблемы.
    if not dm_ok:
        try:
            await db.clear_documents_request(application_id)
        except Exception:
            log.exception("Не удалось откатить отметку запроса документов для заявки %s.", application_id)
        await safe_respond(
            interaction,
            "⚠️ ЛС не доставлено (закрыты или ошибка API). Документы не запрошены — "
            "попробуйте позже или свяжитесь с кандидатом другим способом.",
        )
        log.warning(
            "Не удалось доставить запрос документов пользователю %s (заявка %s).",
            user_id,
            application_id,
        )
        return

    # Обновляем embed заявки, чтобы модераторы видели факт запроса.
    try:
        message = interaction.message
        if message is not None and message.embeds:
            embed = message.embeds[0]
            embed.add_field(
                name="📄 Документы",
                value=f"Запрошены модератором: {interaction.user.mention}",
                inline=False,
            )
            await message.edit(embed=embed)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Не удалось обновить сообщение заявки %s: %s", application_id, exc)

    await safe_respond(
        interaction,
        f"✅ Запрос документов отправлен <@{user_id}> в личные сообщения.\n"
        f"Контакт для отправки: {interaction.user.mention}.",
    )

    log.info(
        "Документы запрошены по заявке %s модератором %s (dm_ok=%s)",
        application_id,
        interaction.user.id,
        dm_ok,
    )


# --------------------------------------------------------------------------- #
# Обработка решения модератора
# --------------------------------------------------------------------------- #
async def _strip_buttons(interaction: discord.Interaction) -> None:
    try:
        if interaction.message is not None:
            await interaction.message.edit(view=None)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Не удалось убрать кнопки с сообщения заявки: %s", exc)


async def apply_role(
    guild: discord.Guild,
    member: Optional[discord.Member],
    role_id: int,
) -> Tuple[bool, str]:
    """Пытается выдать роль. Возвращает (успех, человекочитаемая причина)."""
    if member is None:
        return False, "пользователь не найден на сервере"

    role = guild.get_role(role_id)
    if role is None:
        log.error("Роль %s не найдена на сервере %s.", role_id, guild.id)
        return False, f"роль с ID {role_id} не найдена"

    me = guild.me
    if me is None:
        return False, "бот не найден в кэше сервера"
    if not me.guild_permissions.manage_roles:
        log.error("У бота нет права Manage Roles на сервере %s.", guild.id)
        return False, "у бота нет права «Управление ролями»"
    if role >= me.top_role:
        log.error("Роль %s выше роли бота — выдать невозможно.", role.name)
        return False, f"роль {role.name} выше роли бота в иерархии"

    if role in member.roles:
        return True, "роль уже была выдана"

    try:
        await member.add_roles(role, reason="Рассмотрение заявки в команду")
        return True, "роль выдана"
    except discord.Forbidden:
        log.exception("Forbidden при выдаче роли %s пользователю %s.", role_id, member.id)
        return False, "Discord запретил выдачу роли (права/иерархия)"
    except discord.HTTPException as exc:
        log.exception("HTTPException при выдаче роли %s: %s", role_id, exc)
        return False, "ошибка Discord API при выдаче роли"


async def process_decision(
    interaction: discord.Interaction,
    application_id: int,
    *,
    approve: bool,
) -> None:
    """Общая логика одобрения/отказа. Одна неудачная операция не роняет бота."""
    try:
        await interaction.response.defer(ephemeral=True)
    except (discord.HTTPException, discord.NotFound) as exc:
        log.warning("Interaction устарел: %s", exc)
        return

    guild = interaction.guild
    if guild is None:
        await safe_respond(interaction, "❌ Действие доступно только на сервере.")
        return

    try:
        application = await db.get_application(application_id)
    except Exception:
        log.exception("Ошибка БД при чтении заявки %s.", application_id)
        await safe_respond(interaction, "❌ Внутренняя ошибка базы данных.")
        return

    if application is None:
        await safe_respond(interaction, "❌ Заявка не найдена в базе данных (устаревшее сообщение).")
        await _strip_buttons(interaction)
        return

    if application["status"] != "Pending":
        await safe_respond(interaction, "❌ Эта заявка уже была обработана.")
        await _strip_buttons(interaction)
        return

    new_status = "Approved" if approve else "Rejected"

    # Атомарный «захват» заявки: защищает от двойного нажатия и гонки модераторов.
    try:
        claimed = await db.update_application_status(
            application_id, new_status, expected_status="Pending"
        )
    except Exception:
        log.exception("Ошибка БД при обновлении статуса заявки %s.", application_id)
        await safe_respond(interaction, "❌ Внутренняя ошибка базы данных.")
        return

    if not claimed:
        await safe_respond(interaction, "❌ Эта заявка уже была обработана.")
        await _strip_buttons(interaction)
        return

    user_id = int(application["user_id"])
    member: Optional[discord.Member] = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            log.warning("Пользователь %s покинул сервер до рассмотрения заявки.", user_id)
        except discord.HTTPException as exc:
            log.warning("Не удалось получить участника %s: %s", user_id, exc)

    role_id = config.CANDIDATE_ROLE_ID if approve else config.REJECTED_ROLE_ID
    role_ok, role_reason = await apply_role(guild, member, role_id)

    # Cooldown ставим всегда (даже если роль выдать не удалось): повторная подача
    # блокируется данными в SQLite, а не наличием роли в Discord.
    cooldown_expires: Optional[int] = None
    if not approve:
        cooldown_expires = db.now_ts() + config.cooldown_seconds()
        try:
            await db.create_cooldown(user_id, config.REJECTED_ROLE_ID, cooldown_expires)
        except Exception:
            log.exception("Не удалось сохранить cooldown для пользователя %s.", user_id)

    # Обновляем embed заявки.
    decided_at = dt.datetime.now(dt.timezone.utc)
    try:
        message = interaction.message
        if message is not None and message.embeds:
            embed = message.embeds[0]
            embed.color = COLOR_APPROVED if approve else COLOR_REJECTED
            if approve:
                embed.add_field(
                    name="Решение",
                    value=f"✅ Одобрено модератором: {interaction.user.mention}",
                    inline=False,
                )
            else:
                embed.add_field(
                    name="Решение",
                    value=(
                        f"❌ Отказано модератором: {interaction.user.mention}\n"
                        f"Повторная подача: <t:{cooldown_expires}:f>"
                    ),
                    inline=False,
                )
            embed.add_field(
                name="Дата обработки",
                value=f"<t:{int(decided_at.timestamp())}:F>",
                inline=False,
            )
            if not role_ok:
                embed.add_field(
                    name="⚠️ Внимание",
                    value=f"Роль не выдана: {role_reason}. Выдайте её вручную.",
                    inline=False,
                )
            embed.set_footer(text=f"Статус: {new_status} · ID заявки: {application_id}")
            await message.edit(embed=embed, view=None)
        else:
            await _strip_buttons(interaction)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Не удалось обновить сообщение заявки %s: %s", application_id, exc)

    # ЛС пользователю.
    dm_embed = build_decision_dm_embed(approved=approve, cooldown_expires=cooldown_expires)
    dm_ok = await send_dm(interaction.client, user_id, embed=dm_embed)

    # Итог модератору — честный, без «притворства».
    summary = [f"Заявка #{application_id}: статус **{new_status}**."]
    summary.append("✅ Роль выдана." if role_ok else f"⚠️ Роль НЕ выдана: {role_reason}.")
    summary.append("✅ ЛС отправлено." if dm_ok else "⚠️ ЛС не доставлено (закрыты или ошибка API).")
    if not approve and cooldown_expires:
        summary.append(f"⏳ Cooldown до <t:{cooldown_expires}:f>.")
    await safe_respond(interaction, "\n".join(summary))

    log.info(
        "Заявка %s обработана модератором %s: status=%s, role_ok=%s, dm_ok=%s",
        application_id,
        interaction.user.id,
        new_status,
        role_ok,
        dm_ok,
    )
