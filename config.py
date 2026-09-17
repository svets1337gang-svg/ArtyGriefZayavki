"""Конфигурация бота.

Все значения читаются из переменных окружения (.env), поэтому один и тот же
код можно запускать на разных серверах без правки исходников.
Токен НИКОГДА не хранится в коде и не пишется в логи.
"""

from __future__ import annotations

import os
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    """Ошибка конфигурации: бот не должен стартовать."""


def _get_str(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _get_int(name: str, default: int = 0) -> int:
    """Читает целое число. Некорректное значение -> 0 (поймается валидацией)."""
    raw = _get_str(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return -1  # маркер некорректного значения


def _get_bool(name: str, default: bool = False) -> bool:
    raw = _get_str(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on", "да")


def _get_int_list(name: str) -> List[int]:
    raw = _get_str(name)
    if not raw:
        return []
    result: List[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            result.append(-1)
    return result


# --------------------------------------------------------------------------- #
# Секреты
# --------------------------------------------------------------------------- #
DISCORD_TOKEN: str = _get_str("DISCORD_TOKEN", "") or ""

# --------------------------------------------------------------------------- #
# Идентификаторы Discord
# --------------------------------------------------------------------------- #
GUILD_ID: int = _get_int("GUILD_ID")

APPLICATION_PANEL_CHANNEL_ID: int = _get_int("APPLICATION_PANEL_CHANNEL_ID")
RW_APPLICATION_CHANNEL_ID: int = _get_int("RW_APPLICATION_CHANNEL_ID")
FT_APPLICATION_CHANNEL_ID: int = _get_int("FT_APPLICATION_CHANNEL_ID")

CANDIDATE_ROLE_ID: int = _get_int("CANDIDATE_ROLE_ID")
REJECTED_ROLE_ID: int = _get_int("REJECTED_ROLE_ID")

# --------------------------------------------------------------------------- #
# Логика заявок
# --------------------------------------------------------------------------- #
REJECTION_COOLDOWN_DAYS: int = _get_int("REJECTION_COOLDOWN_DAYS", 7) or 7
MIN_AGE: int = _get_int("MIN_AGE", 14) or 14

# False (по умолчанию): после одобрения пользователь МОЖЕТ подать заявку снова.
# True: одобренная заявка навсегда блокирует повторную подачу.
BLOCK_AFTER_APPROVAL: bool = _get_bool("BLOCK_AFTER_APPROVAL", False)
MAX_AGE: int = 99

# --------------------------------------------------------------------------- #
# Технические настройки
# --------------------------------------------------------------------------- #
DATABASE_PATH: str = _get_str("DATABASE_PATH", "applications.db") or "applications.db"
LOG_FILE: str = _get_str("LOG_FILE", "bot.log") or "bot.log"
LOG_LEVEL: str = (_get_str("LOG_LEVEL", "INFO") or "INFO").upper()
COOLDOWN_CHECK_MINUTES: int = _get_int("COOLDOWN_CHECK_MINUTES", 1) or 1

# Тексты (вынесены сюда, чтобы их можно было менять без правки логики)
PANEL_TITLE = "📝 Заявка в команду проекта"
MODES = ("RW", "FT")


def validate() -> None:
    """Проверяет конфигурацию. Бросает ConfigError с понятным сообщением."""
    problems: List[str] = []

    if not DISCORD_TOKEN:
        problems.append("DISCORD_TOKEN не задан (добавьте его в .env)")

    required_ids = {
        "GUILD_ID": GUILD_ID,
        "APPLICATION_PANEL_CHANNEL_ID": APPLICATION_PANEL_CHANNEL_ID,
        "RW_APPLICATION_CHANNEL_ID": RW_APPLICATION_CHANNEL_ID,
        "FT_APPLICATION_CHANNEL_ID": FT_APPLICATION_CHANNEL_ID,
        "CANDIDATE_ROLE_ID": CANDIDATE_ROLE_ID,
        "REJECTED_ROLE_ID": REJECTED_ROLE_ID,
    }
    for name, value in required_ids.items():
        if value <= 0:
            problems.append(f"{name} не задан или не является корректным Discord ID (снимок: {value})")

    if RW_APPLICATION_CHANNEL_ID > 0 and RW_APPLICATION_CHANNEL_ID == FT_APPLICATION_CHANNEL_ID:
        problems.append("RW_APPLICATION_CHANNEL_ID и FT_APPLICATION_CHANNEL_ID должны различаться")

    if CANDIDATE_ROLE_ID > 0 and CANDIDATE_ROLE_ID == REJECTED_ROLE_ID:
        problems.append("CANDIDATE_ROLE_ID и REJECTED_ROLE_ID должны различаться")

    if REJECTION_COOLDOWN_DAYS <= 0:
        problems.append("REJECTION_COOLDOWN_DAYS должен быть положительным целым числом")

    if MIN_AGE <= 0:
        problems.append("MIN_AGE должен быть положительным целым числом")

    if COOLDOWN_CHECK_MINUTES <= 0:
        problems.append("COOLDOWN_CHECK_MINUTES должен быть положительным целым числом")

    if problems:
        message = "Некорректная конфигурация:\n" + "\n".join(f"  - {p}" for p in problems)
        raise ConfigError(message)


def cooldown_seconds() -> int:
    return REJECTION_COOLDOWN_DAYS * 24 * 60 * 60
