"""WebSocket hub: push статуса агентства клиентам дашборда (Фаза 2.1).

Клиенты подключаются к ``/api/agency/ws`` и получают сообщения:
  ``{"type": "status", "data": <тот же payload, что GET /api/agency/status>}``

Пока есть подписчики, сервер пушит снимок с интервалом
``WS_PUSH_MS_RUNNING`` / ``WS_PUSH_MS_IDLE``. При старте/стопе и т.п.
можно вызвать ``schedule_broadcast`` для немедленного обновления.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Awaitable, Callable, Dict, Optional, Set

logger = logging.getLogger("AgencyWS")


class StatusHub:
    """Множество активных WebSocket-подписчиков статуса."""

    def __init__(self) -> None:
        self._clients: Set[Any] = set()
        self._lock = asyncio.Lock()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def connect(self, websocket: Any) -> None:
        await websocket.accept()
        async with self._lock:
            self._clients.add(websocket)
        logger.info("WS status: клиент подключён (всего %s)", len(self._clients))

    async def disconnect(self, websocket: Any) -> None:
        async with self._lock:
            self._clients.discard(websocket)
        logger.debug("WS status: клиент отключён (всего %s)", len(self._clients))

    async def broadcast_json(self, message: Dict[str, Any]) -> int:
        """Шлёт JSON всем клиентам. Возвращает число успешных отправок."""
        text = json.dumps(message, ensure_ascii=False, default=str)
        async with self._lock:
            clients = list(self._clients)
        sent = 0
        dead: list[Any] = []
        for ws in clients:
            try:
                await ws.send_text(text)
                sent += 1
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)
        return sent


status_hub = StatusHub()

# Инжектируется из main: async () -> dict
_status_builder: Optional[Callable[[], Awaitable[Dict[str, Any]]]] = None
_push_task: Optional[asyncio.Task] = None


def set_status_builder(builder: Callable[[], Awaitable[Dict[str, Any]]]) -> None:
    global _status_builder
    _status_builder = builder


def _push_intervals_ms() -> tuple[int, int]:
    try:
        from core.config import Config
        running = int(getattr(Config, "WS_PUSH_MS_RUNNING", 1500) or 1500)
        idle = int(getattr(Config, "WS_PUSH_MS_IDLE", 10000) or 10000)
        return max(500, running), max(1000, idle)
    except Exception:
        return 1500, 10000


async def broadcast_status_now() -> None:
    """Собрать актуальный статус и разослать подписчикам (если есть)."""
    if status_hub.client_count == 0 or _status_builder is None:
        return
    try:
        payload = await _status_builder()
        await status_hub.broadcast_json({"type": "status", "data": payload})
    except Exception:
        logger.exception("WS broadcast_status_now failed")


def schedule_broadcast() -> None:
    """Неблокирующий триггер из REST-хендлеров."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    loop.create_task(broadcast_status_now())


async def ws_push_loop(is_running: Callable[[], bool]) -> None:
    """Фоновый цикл: push пока есть клиенты."""
    logger.info("WS push loop started")
    while True:
        try:
            running_ms, idle_ms = _push_intervals_ms()
            if status_hub.client_count > 0 and _status_builder is not None:
                await broadcast_status_now()
                delay = (running_ms if is_running() else idle_ms) / 1000.0
            else:
                delay = 2.0
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            logger.info("WS push loop cancelled")
            raise
        except Exception:
            logger.exception("WS push loop error")
            await asyncio.sleep(2.0)


def start_push_loop(is_running: Callable[[], bool]) -> asyncio.Task:
    global _push_task
    if _push_task and not _push_task.done():
        return _push_task
    _push_task = asyncio.create_task(ws_push_loop(is_running))
    return _push_task


async def stop_push_loop() -> None:
    global _push_task
    if _push_task and not _push_task.done():
        _push_task.cancel()
        try:
            await _push_task
        except asyncio.CancelledError:
            pass
    _push_task = None


async def handle_status_websocket(websocket: Any) -> None:
    """Accept → snapshot → читать ping/закрытие."""
    from fastapi import WebSocketDisconnect

    await status_hub.connect(websocket)
    try:
        if _status_builder is not None:
            payload = await _status_builder()
            await websocket.send_text(
                json.dumps(
                    {"type": "status", "data": payload},
                    ensure_ascii=False,
                    default=str,
                )
            )
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and msg.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
            elif isinstance(msg, dict) and msg.get("type") == "refresh":
                await broadcast_status_now()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.debug("WS status closed: %s", e)
    finally:
        await status_hub.disconnect(websocket)
