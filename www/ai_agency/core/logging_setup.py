"""Централизованная настройка логирования агентства.

Цель: в INFO остаются события оркестрации/агентов; poll дашборда (/status)
и рутинные чтения NocoDB не засоряют консоль.
"""
from __future__ import annotations

import logging
import os
from typing import Iterable

# Пути, которые дашборд дергает каждые 2.5–10 с — не пишем в access/INFO.
_QUIET_HTTP_PATHS = frozenset({
    "/api/agency/status",
    "/favicon.ico",
})

_QUIET_PATH_PREFIXES = (
    "/static/",
)


class QuietPollFilter(logging.Filter):
    """Глушит uvicorn access / наш middleware для частых poll-запросов."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        # uvicorn: '192.168.0.123:0 - "GET /api/agency/status HTTP/1.1" 200 OK'
        for path in _QUIET_HTTP_PATHS:
            if path in msg:
                return False
        for prefix in _QUIET_PATH_PREFIXES:
            if f" {prefix}" in msg or f'"{prefix}' in msg or f"'{prefix}" in msg:
                return False
        return True


def is_quiet_http_path(path: str) -> bool:
    if path in _QUIET_HTTP_PATHS:
        return True
    return any(path.startswith(p) for p in _QUIET_PATH_PREFIXES)


def configure_logging() -> None:
    """Вызывать один раз при старте процесса (main.py)."""
    level_name = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()
    level = getattr(logging, level_name, logging.INFO)

    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(
            level=level,
            format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        )
    else:
        root.setLevel(level)

    # Рутина NocoDB (чтение задач/поиск по статусу на каждый poll) — только DEBUG.
    # Важные события (create/update failures) остаются через warning/error
    # или явные info в редких местах.
    noco_level_name = (os.getenv("NOCODB_LOG_LEVEL") or "WARNING").strip().upper()
    noco_level = getattr(logging, noco_level_name, logging.WARNING)
    logging.getLogger("NocoDBClient").setLevel(noco_level)

    # Access-лог uvicorn: без /status
    access = logging.getLogger("uvicorn.access")
    access.addFilter(QuietPollFilter())

    # Шумные HTTP-клиенты
    for name in ("urllib3", "httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)

    logging.getLogger("Main").debug(
        "logging configured: LOG_LEVEL=%s NOCODB_LOG_LEVEL=%s",
        level_name, noco_level_name,
    )
