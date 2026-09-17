"""Точка входа Discord-бота заявок.

Запуск:  python bot.py
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
import database as db
from views import (
    PANEL_CHANNEL_SETTING_KEY,
    PANEL_MESSAGE_SETTING_KEY,
    ApplicationPanelView,
    ApproveButton,
    RejectButton,
)
from web import web_server

log = logging.getLogger("bot")


# --------------------------------------------------------------------------- #
# Логирование
# --------------------------------------------------------------------------- #
def setup_logging() -> None:
    level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    formatter = logging.Formatter(
        "[{asctime}] [{levelname:<8}] {name}: {message}",
        datefmt="%Y-%m-%d %H:%M:%S",
        style="{",
    )

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    try:
        file_handler = logging.handlers.RotatingFileHandler(
            config.LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:  # нет прав на запись файла — работаем только с консолью
        root.warning("Не удалось открыть файл логов %s: %s", config.LOG_FILE, exc)

    # discord.py слишком болтлив на DEBUG
    logging.getLogger("discord").setLevel(max(level, logging.INFO))
    logging.getLogger("discord.http").setLevel(logging.WARNING)


# --------------------------------------------------------------------------- #
# Панель заявок
# --------------------------------------------------------------------------- #
def build_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title=config.PANEL_TITLE,
        description=(
            "Нажми **Подать заявку** и заполни форму.\n\n"
            "**⚠️Потребуется подтверждение возраста**\n"
            "Согласуй, куда отправлять, с администратором, который примет заявку.\n"
            "Дождитесь решения модерации — ответ придёт вам **в личные сообщения**.\n\n"
            "**Важно:**\n"
            f"• Подать заявку можно с **{config.MIN_AGE} лет**.\n"
            "• Потребуется подтверждение возраста.\n"
            f"• После отказа повторная подача доступна через "
            f"**{config.REJECTION_COOLDOWN_DAYS} дней**.\n"
            "• Откройте личные сообщения от участников сервера, иначе вы не получите ответ."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Команда проекта • Набор открыт")
    return embed


async def ensure_application_panel(
    bot: commands.Bot,
    *,
    force_new: bool = False,
) -> Optional[discord.Message]:
    """Гарантирует существование ровно одной панели заявок.

    Ищет сохранённый в SQLite message_id: если сообщение живо — обновляет его,
    если нет — отправляет новое. Дубликаты при перезапуске не создаются.
    """
    channel = bot.get_channel(config.APPLICATION_PANEL_CHANNEL_ID)
    if channel is None:
        try:
            channel = await bot.fetch_channel(config.APPLICATION_PANEL_CHANNEL_ID)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            log.error("Канал панели %s недоступен: %s", config.APPLICATION_PANEL_CHANNEL_ID, exc)
            return None

    if not isinstance(channel, discord.TextChannel):
        log.error("APPLICATION_PANEL_CHANNEL_ID указывает не на текстовый канал.")
        return None

    embed = build_panel_embed()
    view = ApplicationPanelView()

    if not force_new:
        try:
            stored_message_id = await db.get_setting(PANEL_MESSAGE_SETTING_KEY)
            stored_channel_id = await db.get_setting(PANEL_CHANNEL_SETTING_KEY)
        except Exception:
            log.exception("Ошибка БД при чтении данных панели.")
            stored_message_id = stored_channel_id = None

        if stored_message_id and stored_channel_id == str(channel.id):
            try:
                message = await channel.fetch_message(int(stored_message_id))
                await message.edit(embed=embed, view=view)
                log.info("Панель заявок найдена и обновлена (message_id=%s).", message.id)
                return message
            except discord.NotFound:
                log.info("Сохранённая панель удалена — создаю новую.")
            except discord.Forbidden:
                log.error("Нет прав читать/редактировать сообщение панели в канале %s.", channel.id)
                return None
            except (discord.HTTPException, ValueError) as exc:
                log.warning("Не удалось обновить панель: %s", exc)

    try:
        message = await channel.send(embed=embed, view=view)
    except discord.Forbidden:
        log.error("У бота нет прав отправлять сообщения в канал панели %s.", channel.id)
        return None
    except discord.HTTPException as exc:
        log.error("Не удалось отправить панель заявок: %s", exc)
        return None

    try:
        await db.set_setting(PANEL_MESSAGE_SETTING_KEY, str(message.id))
        await db.set_setting(PANEL_CHANNEL_SETTING_KEY, str(channel.id))
    except Exception:
        log.exception("Панель отправлена, но её message_id не сохранён в БД.")

    log.info("Создана новая панель заявок (message_id=%s).", message.id)
    return message


# --------------------------------------------------------------------------- #
# Бот
# --------------------------------------------------------------------------- #
class ApplicationBot(commands.Bot):
    def __init__(self) -> None:
        # Минимально необходимые intents: гильдии (каналы/роли) и участники
        # (получение member и выдача ролей). message_content НЕ нужен.
        intents = discord.Intents.none()
        intents.guilds = True
        intents.members = True

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True),
        )
        self._panel_ready = False

    async def setup_hook(self) -> None:
        await db.init_db()

        # Восстановление persistent-компонентов.
        self.add_view(ApplicationPanelView())
        self.add_dynamic_items(ApproveButton, RejectButton)

        # Синхронизация slash-команд: в гильдии — мгновенно.
        guild = discord.Object(id=config.GUILD_ID)
        try:
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Синхронизировано команд для гильдии %s: %s", config.GUILD_ID, len(synced))
        except discord.HTTPException as exc:
            log.error("Не удалось синхронизировать команды: %s", exc)

        if not self.cooldown_watcher.is_running():
            self.cooldown_watcher.start()

    async def on_ready(self) -> None:
        log.info("Бот запущен как %s (id=%s).", self.user, getattr(self.user, "id", "?"))

        guild = self.get_guild(config.GUILD_ID)
        if guild is None:
            log.error(
                "Бот не состоит в гильдии %s — проверьте GUILD_ID и приглашение бота.",
                config.GUILD_ID,
            )
        else:
            for role_id, name in (
                (config.CANDIDATE_ROLE_ID, "CANDIDATE_ROLE_ID"),
                (config.REJECTED_ROLE_ID, "REJECTED_ROLE_ID"),
            ):
                role = guild.get_role(role_id)
                if role is None:
                    log.error("Роль %s (%s) не найдена на сервере.", role_id, name)
                elif guild.me is not None and role >= guild.me.top_role:
                    log.error(
                        "Роль %s выше роли бота — бот не сможет её выдавать/снимать.", role.name
                    )
            if guild.me is not None and not guild.me.guild_permissions.manage_roles:
                log.error("У бота нет права «Управление ролями» (Manage Roles).")

        if not self._panel_ready:
            self._panel_ready = True
            await ensure_application_panel(self)

    async def close(self) -> None:
        if self.cooldown_watcher.is_running():
            self.cooldown_watcher.cancel()
        await super().close()
        await db.close_db()

    # ----------------------------------------------------------------------- #
    # Фоновая задача снятия роли отказа
    # ----------------------------------------------------------------------- #
    @tasks.loop(minutes=config.COOLDOWN_CHECK_MINUTES)
    async def cooldown_watcher(self) -> None:
        try:
            expired = await db.get_expired_cooldowns()
        except Exception:
            log.exception("Ошибка БД при чтении истёкших cooldown'ов.")
            return

        if not expired:
            return

        guild = self.get_guild(config.GUILD_ID)
        if guild is None:
            log.warning("Гильдия %s недоступна — cooldown'ы будут обработаны позже.", config.GUILD_ID)
            return

        for row in expired:
            user_id = int(row["user_id"])
            role_id = int(row["role_id"])
            try:
                await self._expire_cooldown(guild, user_id, role_id)
            except Exception:
                log.exception("Не удалось обработать cooldown пользователя %s.", user_id)

    async def _expire_cooldown(self, guild: discord.Guild, user_id: int, role_id: int) -> None:
        member: Optional[discord.Member] = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                log.info("Пользователь %s покинул сервер — cooldown удалён.", user_id)
                await db.delete_cooldown(user_id)
                return
            except discord.HTTPException as exc:
                log.warning("Не удалось получить участника %s: %s — повтор позже.", user_id, exc)
                return

        role = guild.get_role(role_id)
        if role is None:
            log.warning("Роль %s не найдена — cooldown пользователя %s удалён.", role_id, user_id)
            await db.delete_cooldown(user_id)
            return

        if role in member.roles:
            try:
                await member.remove_roles(role, reason="Истёк срок ограничения на подачу заявки")
                log.info("Роль %s снята с пользователя %s.", role.name, user_id)
            except discord.Forbidden:
                log.error(
                    "Нет прав снять роль %s с пользователя %s (иерархия/права). Повтор позже.",
                    role.name,
                    user_id,
                )
                return
            except discord.HTTPException as exc:
                log.warning("Ошибка API при снятии роли с %s: %s — повтор позже.", user_id, exc)
                return

        await db.delete_cooldown(user_id)

    @cooldown_watcher.before_loop
    async def before_cooldown_watcher(self) -> None:
        await self.wait_until_ready()

    @cooldown_watcher.error
    async def cooldown_watcher_error(self, error: BaseException) -> None:
        log.exception("Необработанная ошибка в цикле cooldown_watcher.", exc_info=error)


bot = ApplicationBot()


# --------------------------------------------------------------------------- #
# Slash-команды
# --------------------------------------------------------------------------- #
@bot.tree.command(
    name="setup_application_panel",
    description="Создать или обновить панель подачи заявок.",
)
@app_commands.guild_only()
@app_commands.describe(force_new="Отправить новую панель, даже если старая существует.")
async def setup_application_panel(interaction: discord.Interaction, force_new: bool = False) -> None:
    await interaction.response.defer(ephemeral=True)
    message = await ensure_application_panel(bot, force_new=force_new)
    if message is None:
        await interaction.followup.send(
            "❌ Не удалось создать панель. Проверьте APPLICATION_PANEL_CHANNEL_ID и права бота "
            "(подробности в логах).",
            ephemeral=True,
        )
    else:
        await interaction.followup.send(f"✅ Панель готова: {message.jump_url}", ephemeral=True)

@bot.tree.command(
    name="убратькд",
    description="Снять ограничение на подачу заявки с пользователя.",
)
@app_commands.guild_only()
@app_commands.describe(user="Пользователь, с которого нужно снять КД")
async def remove_cooldown(
    interaction: discord.Interaction,
    user: discord.Member,
) -> None:
    await interaction.response.defer(ephemeral=True)

    guild = interaction.guild
    if guild is None:
        await interaction.followup.send("❌ Команда доступна только на сервере.", ephemeral=True)
        return

    # 1. Проверяем, есть ли активный cooldown в БД.
    try:
        cooldown = await db.get_active_cooldown(user.id)
    except Exception:
        log.exception("Ошибка БД при чтении cooldown пользователя %s.", user.id)
        await interaction.followup.send("❌ Внутренняя ошибка базы данных.", ephemeral=True)
        return

    if cooldown is None:
        await interaction.followup.send(
            f"ℹ️ У пользователя {user.mention} нет активного КД.",
            ephemeral=True,
        )
        return

    # 2. Удаляем запись из БД.
    try:
        await db.delete_cooldown(user.id)
    except Exception:
        log.exception("Не удалось удалить cooldown пользователя %s.", user.id)
        await interaction.followup.send(
            "❌ Не удалось снять КД (ошибка БД).", ephemeral=True
        )
        return

    # 3. Пытаемся снять роль отказа, если она есть.
    role_ok = True
    role_reason = ""
    role = guild.get_role(config.REJECTED_ROLE_ID)
    if role is None:
        role_ok = False
        role_reason = f"роль с ID {config.REJECTED_ROLE_ID} не найдена"
    elif role not in user.roles:
        role_reason = "роль отказа и так отсутствовала"
    else:
        me = guild.me
        if me is None:
            role_ok = False
            role_reason = "бот не найден в кэше сервера"
        elif not me.guild_permissions.manage_roles:
            role_ok = False
            role_reason = "у бота нет права «Управление ролями»"
        elif role >= me.top_role:
            role_ok = False
            role_reason = f"роль {role.name} выше роли бота в иерархии"
        else:
            try:
                await user.remove_roles(role, reason=f"КД снят модератором {interaction.user}")
                role_reason = "роль снята"
            except discord.Forbidden:
                log.exception("Forbidden при снятии роли %s с %s.", role.id, user.id)
                role_ok = False
                role_reason = "Discord запретил снятие роли (права/иерархия)"
            except discord.HTTPException as exc:
                log.exception("HTTPException при снятии роли %s: %s", role.id, exc)
                role_ok = False
                role_reason = "ошибка Discord API при снятии роли"

    # 4. Ответ модератору.
    summary = [f"✅ КД снят с {user.mention} (ID: `{user.id}`)."]
    if role_ok:
        summary.append(f"Роль отказа: {role_reason}." if role_reason else "Роль отказа снята.")
    else:
        summary.append(f"⚠️ Роль НЕ снята: {role_reason}. Снимите вручную.")

    await interaction.followup.send("\n".join(summary), ephemeral=True)

    log.info(
        "КД снят с пользователя %s модератором %s (role_ok=%s, reason=%s)",
        user.id,
        interaction.user.id,
        role_ok,
        role_reason,
    )

@bot.tree.error
async def on_app_command_error(
    interaction: discord.Interaction,
    error: app_commands.AppCommandError,
) -> None:
    if isinstance(error, app_commands.MissingPermissions):
        message = "❌ У вас нет прав для использования этой команды."
    elif isinstance(error, app_commands.BotMissingPermissions):
        message = "❌ У бота недостаточно прав для выполнения команды."
    elif isinstance(error, app_commands.NoPrivateMessage):
        message = "❌ Команда доступна только на сервере."
    else:
        log.exception("Ошибка slash-команды: %s", error, exc_info=error)
        message = "❌ Произошла ошибка при выполнении команды."

    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except (discord.HTTPException, discord.NotFound):
        pass


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> None:
    setup_logging()

    try:
        config.validate()
    except config.ConfigError as exc:
        logging.getLogger("config").error("%s", exc)
        print(f"\n{exc}\n\nИсправьте .env и запустите бота снова.", file=sys.stderr)
        raise SystemExit(1)

    try:
        # log_handler=None — логирование уже настроено нами выше.
        bot.run(config.DISCORD_TOKEN, log_handler=None)
    except discord.LoginFailure:
        log.error("Неверный DISCORD_TOKEN — Discord отклонил авторизацию.")
        raise SystemExit(1)
    except discord.PrivilegedIntentsRequired:
        log.error(
            "Не включён privileged intent SERVER MEMBERS. "
            "Включите его в Discord Developer Portal -> Bot -> Privileged Gateway Intents."
        )
        raise SystemExit(1)
    except KeyboardInterrupt:
        log.info("Остановка по Ctrl+C.")


if __name__ == "__main__":
    main()
