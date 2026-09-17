"""Modal подачи заявки и серверная валидация введённых данных."""

from __future__ import annotations

import datetime as dt
import logging
import re
from typing import Optional

import discord

import config
import database as db
from views import (
    ModerationView,
    build_application_embed,
    check_eligibility,
    safe_respond,
)

log = logging.getLogger(__name__)

_WHITESPACE_RE = re.compile(r"\s+")
_DIGITS_RE = re.compile(r"^\d{1,3}$")
_EMBED_FIELD_LIMIT = 1000


def normalize_line(value: str) -> str:
    """Схлопывает пробелы и обрезает строку (однострочные поля)."""
    return _WHITESPACE_RE.sub(" ", value).strip()


def normalize_text(value: str, limit: int = _EMBED_FIELD_LIMIT) -> str:
    """Нормализует многострочный текст и ограничивает длину под лимиты Embed."""
    lines = [line.rstrip() for line in value.replace("\r\n", "\n").split("\n")]
    text = "\n".join(line for line in lines).strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text or "—"


def parse_age(raw: str) -> Optional[int]:
    """Возвращает возраст как int или None, если это не целое число."""
    cleaned = normalize_line(raw).replace(" ", "")
    if not _DIGITS_RE.match(cleaned):
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


class ApplicationModal(discord.ui.Modal, title="Заявка в команду проекта"):
    """Форма заявки. Режим приходит из Select Menu и сохраняется вместе с заявкой."""

    nickname: discord.ui.TextInput = discord.ui.TextInput(
        label="Ваш ник",
        placeholder="Например: unheardvoice",
        style=discord.TextStyle.short,
        required=True,
        max_length=64,
    )
    age: discord.ui.TextInput = discord.ui.TextInput(
        label="Сколько вам лет. (От: 14 лет)",
        placeholder="Например: 18",
        style=discord.TextStyle.short,
        required=True,
        max_length=3,
    )
    timezone: discord.ui.TextInput = discord.ui.TextInput(
        label="Часовой пояс",
        placeholder="Например: UTC+3 / МСК",
        style=discord.TextStyle.short,
        required=True,
        max_length=64,
    )
    blacklist: discord.ui.TextInput = discord.ui.TextInput(
        label="Были ли вы в ЧСП/ЧСС? Если да, то за что",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=900,
    )
    motivation: discord.ui.TextInput = discord.ui.TextInput(
        label="Почему мы должны взять именно вас?",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=900,
    )

    def __init__(self, *, mode: str, origin_interaction: Optional[discord.Interaction] = None) -> None:
        super().__init__(timeout=600)
        self.mode = mode
        self.origin_interaction = origin_interaction
        self.title = f"Заявка в команду · {mode}"

    async def _cleanup_origin(self) -> None:
        """Убирает ephemeral-сообщение с Select Menu, чтобы не мусорить в UX."""
        if self.origin_interaction is None:
            return
        try:
            await self.origin_interaction.delete_original_response()
        except (discord.HTTPException, discord.NotFound):
            pass

    async def on_submit(self, interaction: discord.Interaction) -> None:  # noqa: C901
        user = interaction.user
        guild = interaction.guild

        if guild is None:
            await safe_respond(interaction, "❌ Подать заявку можно только на сервере.")
            return

        # --- Серверная валидация (UI-ограничениям не доверяем) ----------------
        age_value = parse_age(self.age.value)
        if age_value is None:
            await interaction.response.send_message("❌ Укажите возраст целым числом.", ephemeral=True)
            return
        if age_value < config.MIN_AGE:
            await interaction.response.send_message(
                f"❌ Подать заявку можно только с {config.MIN_AGE} лет.", ephemeral=True
            )
            return
        if age_value > config.MAX_AGE:
            await interaction.response.send_message("❌ Укажите корректный возраст.", ephemeral=True)
            return

        nickname_value = normalize_line(self.nickname.value)
        timezone_value = normalize_line(self.timezone.value)
        blacklist_value = normalize_text(self.blacklist.value)
        motivation_value = normalize_text(self.motivation.value)

        if not nickname_value or not timezone_value:
            await interaction.response.send_message(
                "❌ Поля не могут состоять только из пробелов.", ephemeral=True
            )
            return

        # Дальше идут обращения к БД и Discord API — откладываем ответ,
        # чтобы гарантированно уложиться в 3-секундный лимит interaction.
        try:
            await interaction.response.defer(ephemeral=True)
        except (discord.HTTPException, discord.NotFound) as exc:
            log.warning("Interaction формы устарел: %s", exc)
            return

        # --- Повторная проверка прав на подачу (защита от гонок) --------------
        try:
            allowed, reason = await check_eligibility(user.id)
        except Exception:
            log.exception("Ошибка БД при проверке права на подачу заявки (modal).")
            await interaction.followup.send(
                "❌ Внутренняя ошибка. Попробуйте позже.", ephemeral=True
            )
            return

        if not allowed:
            await interaction.followup.send(
                reason or "❌ Сейчас подать заявку нельзя.", ephemeral=True
            )
            await self._cleanup_origin()
            return

        # --- Канал назначения --------------------------------------------------
        channel_id = (
            config.RW_APPLICATION_CHANNEL_ID
            if self.mode == "RW"
            else config.FT_APPLICATION_CHANNEL_ID
        )
        channel = interaction.client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await interaction.client.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                log.error("Канал заявок %s недоступен: %s", channel_id, exc)
                channel = None

        if not isinstance(channel, discord.abc.Messageable):
            await interaction.followup.send(
                "❌ Канал для заявок не настроен. Сообщите администрации.", ephemeral=True
            )
            return

        # --- Создание заявки ---------------------------------------------------
        created_at = dt.datetime.now(dt.timezone.utc)
        try:
            application_id = await db.create_application(
                user.id, self.mode, int(created_at.timestamp())
            )
        except Exception:
            log.exception("Не удалось создать заявку в БД для пользователя %s.", user.id)
            await interaction.followup.send(
                "❌ Внутренняя ошибка базы данных. Попробуйте позже.", ephemeral=True
            )
            return

        embed = build_application_embed(
            application_id=application_id,
            user=user,
            mode=self.mode,
            nickname=nickname_value,
            age=age_value,
            timezone_text=timezone_value,
            blacklist_text=blacklist_value,
            motivation_text=motivation_value,
            created_at=created_at,
        )

        try:
            message = await channel.send(embed=embed, view=ModerationView(application_id))
        except discord.Forbidden:
            log.error("Нет прав отправить заявку в канал %s.", channel_id)
            await db.delete_application(application_id)
            await interaction.followup.send(
                "❌ У бота нет прав писать в канал заявок. Сообщите администрации.", ephemeral=True
            )
            return
        except discord.HTTPException as exc:
            log.exception("Ошибка Discord API при отправке заявки: %s", exc)
            await db.delete_application(application_id)
            await interaction.followup.send(
                "❌ Не удалось отправить заявку. Попробуйте позже.", ephemeral=True
            )
            return

        try:
            await db.set_application_message_id(application_id, message.id)
        except Exception:
            log.exception(
                "Заявка %s отправлена (message_id=%s), но message_id не сохранён.",
                application_id,
                message.id,
            )

        await interaction.followup.send(
            "✅ **Заявка успешно отправлена!**\n\n"
            "Ваша заявка принята и ожидает рассмотрения администрацией.\n"
            "После проверки с вами свяжутся в **личных сообщениях**.\n\n"
            "Спасибо за проявленный интерес к нашей команде!",
            ephemeral=True,
        )
        await self._cleanup_origin()

        log.info(
            "Создана заявка %s: user=%s mode=%s message=%s",
            application_id,
            user.id,
            self.mode,
            message.id,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Необработанная ошибка в ApplicationModal: %s", error)
        await safe_respond(
            interaction,
            "❌ Произошла ошибка при обработке формы. Попробуйте ещё раз позже.",
        )
