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
from typing import Optional, Tuple

import discord

import config
import database as db

log = logging.getLogger(__name__)

COLOR_PENDING = discord.Color.blurple()
COLOR_APPROVED = discord.Color.green()
COLOR_REJECTED = discord.Color.red()

PANEL_BUTTON_CUSTOM_ID = "application_panel:open"
PANEL_MESSAGE_SETTING_KEY = "panel_message_id"
PANEL_CHANNEL_SETTING_KEY = "panel_channel_id"


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


async def check_eligibility(user_id: int) -> Tuple[bool, Optional[str]]:
    """Можно ли пользователю подать новую заявку.

    Порядок проверок: Pending -> активный cooldown -> (опц.) Approved.

    Одобренная заявка по умолчанию НЕ блокирует новую подачу: пользователь может
    подать заявку повторно (например, на второй режим или после ухода из команды).
    Поведение переключается флагом config.BLOCK_AFTER_APPROVAL.
    """
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


async def send_dm(bot: discord.Client, user_id: int, content: str) -> bool:
    """Пытается отправить ЛС. Возвращает False, если ЛС закрыты/пользователь удалён."""
    try:
        user = bot.get_user(user_id) or await bot.fetch_user(user_id)
    except (discord.NotFound, discord.HTTPException) as exc:
        log.warning("Не удалось получить пользователя %s для ЛС: %s", user_id, exc)
        return False

    try:
        await user.send(content)
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

        try:
            allowed, reason = await check_eligibility(interaction.user.id)
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


class ModerationView(discord.ui.View):
    """View с кнопками решения. Нужен только для первичной отправки сообщения:
    после перезапуска кнопки восстанавливаются через bot.add_dynamic_items()."""

    def __init__(self, application_id: int) -> None:
        super().__init__(timeout=None)
        self.add_item(ApproveButton(application_id))
        self.add_item(RejectButton(application_id))

# --------------------------------------------------------------------------- #
# Обработка решения модератора
# --------------------------------------------------------------------------- #
async def _strip_buttons(interaction: discord.Interaction) -> None:
    try:
        if interaction.message is not None:
            await interaction.message.edit(view=None)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Не удалось убрать кнопки с сообщения заявки: %s", exc)


async def _apply_role(
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
    role_ok, role_reason = await _apply_role(guild, member, role_id)

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
    if approve:
        dm_text = (
            "✅ Заявка одобрена\n\n"
            "Ваша заявка в команду проекта была одобрена. Вам выдана роль «Кандидат».\n\n"
            "Для дальнейшего прохождения отбора необходимо записаться на обзвон "
            "в соответствующем канале."
        )
    else:
        dm_text = (
            "❌ Заявка отклонена\n\n"
            "К сожалению, ваша заявка в команду проекта была отклонена.\n\n"
            f"Подать новую заявку можно <t:{cooldown_expires}:R> (<t:{cooldown_expires}:f>)."
        )

    dm_ok = await send_dm(interaction.client, user_id, dm_text)

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
