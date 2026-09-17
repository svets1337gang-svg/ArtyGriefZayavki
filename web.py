"""Мини-HTTP-сервер для Render Web Service.

Render требует, чтобы контейнер слушал порт $PORT, иначе деплой падает
с ошибкой "no open ports detected".

Здесь поднимается aiohttp-сервер в том же event loop, что и бот:
  * отдаёт 200 OK на / и /health — этого достаточно для healthcheck Render;
  * фоновый self-ping раз в WEB_SELF_PING_MINUTES будит контейнер,
    чтобы бесплатный план не засыпал (актуально для Render Free).

Порт берётся из $PORT (Render задаёт его сам). Если переменная не задана,
используется WEB_PORT или 8080 — удобно для локального запуска.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from aiohttp import web

log = logging.getLogger("web")

# --------------------------------------------------------------------------- #
# Конфигурация (читается из env, чтобы не плодить зависимости от config.py)
# --------------------------------------------------------------------------- #
def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# Render задаёт PORT автоматически. Для локалки — WEB_PORT или 8080.
WEB_PORT: int = _env_int("PORT", 0) or _env_int("WEB_PORT", 8080)

# Публичный URL сервиса для self-ping (например, https://my-bot.onrender.com).
# Если пусто — self-ping выключен.
WEB_PUBLIC_URL: str = (os.getenv("WEB_PUBLIC_URL") or "").strip().rstrip("/")

# Интервал self-ping в минутах. 14 минут — Render Free засыпает после 15 минут
# без трафика, так что 14 — с запасом.
WEB_SELF_PING_MINUTES: int = _env_int("WEB_SELF_PING_MINUTES", 14)


# --------------------------------------------------------------------------- #
# HTTP-обработчики
# --------------------------------------------------------------------------- #
async def handle_root(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "status": "ok",
            "service": "discord-application-bot",
            "message": "Bot is running. See /health for details.",
        }
    )


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "healthy"})


async def handle_ping(request: web.Request) -> web.Response:
    """Отдельный эндпоинт для внешних пингеров (cron-job.org, UptimeRobot)."""
    return web.Response(text="pong")


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/", handle_root)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/ping", handle_ping)
    return app


# --------------------------------------------------------------------------- #
# Жизненный цикл сервера
# --------------------------------------------------------------------------- #
class WebServer:
    """Управляет aiohttp-сервером и self-ping'ом внутри event loop бота."""

    def __init__(self) -> None:
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._self_ping_task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        app = build_app()
        self._runner = web.AppRunner(app)
        await self._runner.setup()

        # 0.0.0.0 обязателен: Render проксирует снаружи.
        self._site = web.TCPSite(self._runner, host="0.0.0.0", port=WEB_PORT)
        await self._site.start()
        log.info("HTTP-сервер запущен на 0.0.0.0:%s", WEB_PORT)

        if WEB_PUBLIC_URL:
            self._self_ping_task = asyncio.create_task(self._self_ping_loop())
            log.info(
                "Self-ping включён: %s каждые %s мин.",
                WEB_PUBLIC_URL,
                WEB_SELF_PING_MINUTES,
            )
        else:
            log.info(
                "WEB_PUBLIC_URL не задан — self-ping выключен. "
                "Настройте внешний пингер на %s/ping.",
                "https://<ваш-сервис>.onrender.com",
            )

    async def stop(self) -> None:
        if self._self_ping_task is not None:
            self._self_ping_task.cancel()
            try:
                await self._self_ping_task
            except (asyncio.CancelledError, Exception):
                pass
            self._self_ping_task = None

        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            log.info("HTTP-сервер остановлен.")

    async def _self_ping_loop(self) -> None:
        """Пингует /ping каждые N минут, чтобы Render Free не засыпал."""
        import aiohttp  # локальный импорт: не нужен, если self-ping выключен

        url = f"{WEB_PUBLIC_URL}/ping"
        interval = max(1, WEB_SELF_PING_MINUTES) * 60

        # Небольшая задержка при старте — дать серверу подняться.
        await asyncio.sleep(30)

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            while True:
                try:
                    async with session.get(url) as resp:
                        log.debug("Self-ping %s -> %s", url, resp.status)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("Self-ping не удался: %s", exc)
                await asyncio.sleep(interval)


# Глобальный экземпляр — импортируется из bot.py
web_server = WebServer()
