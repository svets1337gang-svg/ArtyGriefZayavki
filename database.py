"""Слой работы с SQLite.

Здесь нет ни одного импорта discord — SQL-логика полностью отделена от UI.

Стандартный модуль sqlite3 синхронный, поэтому все обращения выполняются
в отдельном потоке через asyncio.to_thread и сериализуются asyncio.Lock,
чтобы не блокировать event loop бота и не ловить гонки на одном соединении.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from typing import Any, Dict, List, Optional, Sequence

import config

log = logging.getLogger(__name__)

VALID_MODES = ("RW", "FT")
VALID_STATUSES = ("Pending", "Approved", "Rejected")

_connection: Optional[sqlite3.Connection] = None
_lock = asyncio.Lock()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    mode       TEXT    NOT NULL CHECK (mode IN ('RW', 'FT')),
    status     TEXT    NOT NULL CHECK (status IN ('Pending', 'Approved', 'Rejected')),
    message_id INTEGER,
    timestamp  INTEGER NOT NULL,
    documents_requested_by INTEGER,
    documents_requested_at INTEGER
);

CREATE INDEX IF NOT EXISTS idx_applications_user   ON applications (user_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS idx_applications_message
    ON applications (message_id) WHERE message_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS cooldowns (
    user_id    INTEGER PRIMARY KEY,
    role_id    INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cooldowns_expires ON cooldowns (expires_at);

-- Служебная таблица: хранит id сообщения-панели, чтобы не плодить дубликаты.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_ts() -> int:
    """Текущее время в Unix timestamp (UTC, целое число секунд)."""
    return int(time.time())


# --------------------------------------------------------------------------- #
# Внутренние помощники
# --------------------------------------------------------------------------- #
def _get_connection() -> sqlite3.Connection:
    global _connection
    if _connection is None:
        _connection = sqlite3.connect(
            config.DATABASE_PATH,
            check_same_thread=False,
            timeout=30,
        )
        _connection.row_factory = sqlite3.Row
        _connection.execute("PRAGMA journal_mode=WAL")
        _connection.execute("PRAGMA synchronous=NORMAL")
    return _connection


async def _run(func, *args):
    """Выполняет синхронную функцию работы с БД в отдельном потоке."""
    async with _lock:
        return await asyncio.to_thread(func, *args)


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


# --------------------------------------------------------------------------- #
# Инициализация / завершение
# --------------------------------------------------------------------------- #
def _init_db() -> None:
    conn = _get_connection()
    with conn:
        conn.executescript(_SCHEMA)
    _migrate_applications_columns()


# Колонки, добавленные позже базовой схемы. На существующей БД их добирает
# ALTER TABLE: CREATE TABLE IF NOT EXISTS уже созданную таблицу не трогает.
_APPLICATION_MIGRATIONS = (
    ("documents_requested_by", "INTEGER"),
    ("documents_requested_at", "INTEGER"),
)


def _migrate_applications_columns() -> None:
    conn = _get_connection()
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(applications)")}
    for column, column_type in _APPLICATION_MIGRATIONS:
        if column in existing:
            continue
        with conn:
            conn.execute(f"ALTER TABLE applications ADD COLUMN {column} {column_type}")
        log.info("Миграция БД: в applications добавлена колонка %s.", column)


async def init_db() -> None:
    """Создаёт таблицы при запуске (идемпотентно)."""
    await _run(_init_db)
    log.info("База данных инициализирована: %s", config.DATABASE_PATH)


def _close_db() -> None:
    global _connection
    if _connection is not None:
        _connection.close()
        _connection = None


async def close_db() -> None:
    await _run(_close_db)


# --------------------------------------------------------------------------- #
# applications
# --------------------------------------------------------------------------- #
def _create_application(user_id: int, mode: str, timestamp: int) -> int:
    conn = _get_connection()
    with conn:
        cursor = conn.execute(
            "INSERT INTO applications (user_id, mode, status, message_id, timestamp) "
            "VALUES (?, ?, 'Pending', NULL, ?)",
            (user_id, mode, timestamp),
        )
    return int(cursor.lastrowid)


async def create_application(user_id: int, mode: str, timestamp: Optional[int] = None) -> int:
    """Создаёт заявку со статусом Pending и возвращает её id."""
    if mode not in VALID_MODES:
        raise ValueError(f"Недопустимый режим: {mode!r}")
    return await _run(_create_application, user_id, mode, timestamp or now_ts())


def _delete_application(application_id: int) -> None:
    conn = _get_connection()
    with conn:
        conn.execute("DELETE FROM applications WHERE id = ?", (application_id,))


async def delete_application(application_id: int) -> None:
    """Откат: используется, если заявку не удалось отправить модераторам."""
    await _run(_delete_application, application_id)


def _get_application(application_id: int):
    conn = _get_connection()
    return conn.execute("SELECT * FROM applications WHERE id = ?", (application_id,)).fetchone()


async def get_application(application_id: int) -> Optional[Dict[str, Any]]:
    return _row_to_dict(await _run(_get_application, application_id))


def _get_pending_application(user_id: int):
    conn = _get_connection()
    return conn.execute(
        "SELECT * FROM applications WHERE user_id = ? AND status = 'Pending' "
        "ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()


async def get_pending_application(user_id: int) -> Optional[Dict[str, Any]]:
    return _row_to_dict(await _run(_get_pending_application, user_id))


def _get_last_application(user_id: int):
    conn = _get_connection()
    return conn.execute(
        "SELECT * FROM applications WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (user_id,),
    ).fetchone()


async def get_last_application(user_id: int) -> Optional[Dict[str, Any]]:
    return _row_to_dict(await _run(_get_last_application, user_id))


def _get_application_by_message_id(message_id: int):
    conn = _get_connection()
    return conn.execute(
        "SELECT * FROM applications WHERE message_id = ?", (message_id,)
    ).fetchone()


async def get_application_by_message_id(message_id: int) -> Optional[Dict[str, Any]]:
    return _row_to_dict(await _run(_get_application_by_message_id, message_id))


def _get_status_counts():
    conn = _get_connection()
    return conn.execute(
        "SELECT status, COUNT(*) AS count FROM applications GROUP BY status"
    ).fetchall()


async def get_status_counts() -> Dict[str, int]:
    """Количество заявок по статусам: {'Pending': 2, 'Approved': 5, ...}."""
    rows: Sequence[sqlite3.Row] = await _run(_get_status_counts)
    return {row["status"]: int(row["count"]) for row in rows}


def _get_mode_counts():
    conn = _get_connection()
    return conn.execute(
        "SELECT mode, COUNT(*) AS count FROM applications GROUP BY mode"
    ).fetchall()


async def get_mode_counts() -> Dict[str, int]:
    """Количество заявок по режимам: {'RW': 3, 'FT': 4}."""
    rows: Sequence[sqlite3.Row] = await _run(_get_mode_counts)
    return {row["mode"]: int(row["count"]) for row in rows}


def _get_pending_applications():
    conn = _get_connection()
    return conn.execute(
        "SELECT * FROM applications WHERE status = 'Pending' ORDER BY timestamp, id"
    ).fetchall()


async def get_pending_applications() -> List[Dict[str, Any]]:
    """Все заявки со статусом Pending, старейшие первыми."""
    rows: Sequence[sqlite3.Row] = await _run(_get_pending_applications)
    return [dict(row) for row in rows]


def _update_application_status(application_id: int, status: str, expected_status: Optional[str]) -> bool:
    conn = _get_connection()
    with conn:
        if expected_status is None:
            cursor = conn.execute(
                "UPDATE applications SET status = ? WHERE id = ?",
                (status, application_id),
            )
        else:
            cursor = conn.execute(
                "UPDATE applications SET status = ? WHERE id = ? AND status = ?",
                (status, application_id, expected_status),
            )
    return cursor.rowcount > 0


async def update_application_status(
    application_id: int,
    status: str,
    expected_status: Optional[str] = None,
) -> bool:
    """Меняет статус заявки.

    Если передан expected_status, обновление выполняется атомарно только при
    совпадении текущего статуса. Возвращает True, если строка действительно
    обновлена — это и есть защита от двойного нажатия кнопки модерации.
    """
    if status not in VALID_STATUSES:
        raise ValueError(f"Недопустимый статус: {status!r}")
    return await _run(_update_application_status, application_id, status, expected_status)


def _set_application_message_id(application_id: int, message_id: int) -> None:
    conn = _get_connection()
    with conn:
        conn.execute(
            "UPDATE applications SET message_id = ? WHERE id = ?",
            (message_id, application_id),
        )


async def set_application_message_id(application_id: int, message_id: int) -> None:
    await _run(_set_application_message_id, application_id, message_id)


def _mark_documents_requested(application_id: int, moderator_id: int, moment: int) -> bool:
    conn = _get_connection()
    with conn:
        cursor = conn.execute(
            "UPDATE applications "
            "SET documents_requested_by = ?, documents_requested_at = ? "
            "WHERE id = ? AND documents_requested_at IS NULL",
            (moderator_id, moment, application_id),
        )
    return cursor.rowcount > 0


async def mark_documents_requested(
    application_id: int,
    moderator_id: int,
    moment: Optional[int] = None,
) -> bool:
    """Отмечает, что документы запрошены. Возвращает False, если уже были запрошены.

    Атомарность (WHERE documents_requested_at IS NULL) защищает от двойного
    нажатия кнопки — так же, как смена статуса заявки.
    """
    return await _run(_mark_documents_requested, application_id, moderator_id, moment or now_ts())


def _get_documents_request(application_id: int):
    conn = _get_connection()
    return conn.execute(
        "SELECT documents_requested_by, documents_requested_at "
        "FROM applications WHERE id = ?",
        (application_id,),
    ).fetchone()


async def get_documents_request(application_id: int) -> Optional[Dict[str, Any]]:
    """Возвращает {documents_requested_by, documents_requested_at} или None."""
    return _row_to_dict(await _run(_get_documents_request, application_id))


def _get_documents_request_count():
    conn = _get_connection()
    return conn.execute(
        "SELECT COUNT(*) AS count FROM applications WHERE documents_requested_at IS NOT NULL"
    ).fetchone()


async def get_documents_request_count() -> int:
    """Сколько заявок получили запрос документов."""
    row = await _run(_get_documents_request_count)
    return int(row["count"]) if row is not None else 0


def _clear_documents_request(application_id: int) -> None:
    conn = _get_connection()
    with conn:
        conn.execute(
            "UPDATE applications "
            "SET documents_requested_by = NULL, documents_requested_at = NULL "
            "WHERE id = ?",
            (application_id,),
        )


async def clear_documents_request(application_id: int) -> None:
    """Откат: снимает отметку, если ЛС с запросом документов не доставлено."""
    await _run(_clear_documents_request, application_id)


# --------------------------------------------------------------------------- #
# cooldowns
# --------------------------------------------------------------------------- #
def _create_cooldown(user_id: int, role_id: int, expires_at: int) -> None:
    conn = _get_connection()
    with conn:
        conn.execute(
            "INSERT INTO cooldowns (user_id, role_id, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET role_id = excluded.role_id, "
            "expires_at = excluded.expires_at",
            (user_id, role_id, expires_at),
        )


async def create_cooldown(user_id: int, role_id: int, expires_at: int) -> None:
    await _run(_create_cooldown, user_id, role_id, expires_at)


def _get_active_cooldown(user_id: int, moment: int):
    conn = _get_connection()
    return conn.execute(
        "SELECT * FROM cooldowns WHERE user_id = ? AND expires_at > ?",
        (user_id, moment),
    ).fetchone()


async def get_active_cooldown(user_id: int, moment: Optional[int] = None) -> Optional[Dict[str, Any]]:
    return _row_to_dict(await _run(_get_active_cooldown, user_id, moment or now_ts()))


def _get_expired_cooldowns(moment: int):
    conn = _get_connection()
    return conn.execute(
        "SELECT * FROM cooldowns WHERE expires_at <= ? ORDER BY expires_at",
        (moment,),
    ).fetchall()


async def get_expired_cooldowns(moment: Optional[int] = None) -> List[Dict[str, Any]]:
    rows: Sequence[sqlite3.Row] = await _run(_get_expired_cooldowns, moment or now_ts())
    return [dict(row) for row in rows]


def _delete_cooldown(user_id: int) -> None:
    conn = _get_connection()
    with conn:
        conn.execute("DELETE FROM cooldowns WHERE user_id = ?", (user_id,))


async def delete_cooldown(user_id: int) -> None:
    await _run(_delete_cooldown, user_id)


# --------------------------------------------------------------------------- #
# settings (служебное хранилище, например id сообщения-панели)
# --------------------------------------------------------------------------- #
def _get_setting(key: str):
    conn = _get_connection()
    return conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()


async def get_setting(key: str) -> Optional[str]:
    row = await _run(_get_setting, key)
    return row["value"] if row is not None else None


def _set_setting(key: str, value: str) -> None:
    conn = _get_connection()
    with conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


async def set_setting(key: str, value: str) -> None:
    await _run(_set_setting, key, str(value))


async def get_recruitment_status() -> bool:
    """Возвращает True, если набор открыт (по умолчанию открыт)."""
    status = await get_setting("recruitment_open")
    if status is None:
        return True  # По умолчанию набор открыт
    return status.lower() in ("1", "true", "yes")


async def set_recruitment_status(is_open: bool) -> None:
    """Устанавливает статус набора (открыт/закрыт)."""
    await set_setting("recruitment_open", "true" if is_open else "false")
