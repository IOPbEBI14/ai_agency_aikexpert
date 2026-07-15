"""
Клиент self-hosted OpenSERP (https://github.com/karust/openserp).

OpenSERP — бесплатный open-source SERP API (Google, Yandex, Bing, DuckDuckGo,
Baidu, Ecosia), не требующий платных API-ключей. Используется как основной
источник поиска для агента client_hunter вместо/вместе с платным Google Custom
Search API (см. core/client_hunter_tools.py).

Запуск сервера (см. readme.md → OpenSERP):
    docker run --rm -p 127.0.0.1:7000:7000 karust/openserp:latest serve -a 0.0.0.0 -p 7000
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import requests

from core.config import Config

logger = logging.getLogger("OpenSerpClient")


class OpenSerpClient:
    """Тонкий HTTP-клиент к self-hosted OpenSERP-серверу.

    Согласно чек-листу устойчивости: недоступность OpenSERP (timeout, connection
    error, 5xx) — временная ошибка, не бросаем исключение наружу, а возвращаем
    пустой список, чтобы вызывающий код мог перейти на резервный сценарий
    (fallback на Google Custom Search API в client_hunter_tools).
    """

    def __init__(self, base_url: Optional[str] = None, timeout: Optional[int] = None) -> None:
        self.base_url = (base_url or Config.OPENSERP_BASE_URL or "").rstrip("/")
        self.timeout = timeout or Config.OPENSERP_TIMEOUT_SEC

    def is_configured(self) -> bool:
        return bool(self.base_url)

    def search(
        self,
        query: str,
        engine: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, str]]:
        """Ищет через OpenSERP. Возвращает [{title, link, snippet}].

        Пустой список при: сервер не настроен, недоступен, вернул ошибку/невалидный JSON.
        Никогда не бросает исключение — вызывающий код всегда получает список (может пустой).
        """
        query = (query or "").strip()
        if not query or not self.is_configured():
            return []

        engine = engine or Config.OPENSERP_ENGINE
        url = f"{self.base_url}/{engine}/search"
        params = {"text": query, "limit": max(1, min(limit, 100))}

        try:
            response = requests.get(url, params=params, timeout=self.timeout)
        except requests.exceptions.RequestException as e:
            logger.warning(f"⚠️ OpenSERP недоступен ({self.base_url}): {e}")
            return []

        if response.status_code != 200:
            logger.warning(
                "⚠️ OpenSERP HTTP %s для запроса %r (engine=%s): %s",
                response.status_code, query[:80], engine, response.text[:200],
            )
            return []

        try:
            data = response.json()
        except ValueError as e:
            logger.warning(f"⚠️ OpenSERP вернул невалидный JSON: {e}")
            return []

        results = self._parse_results(data)
        logger.info(f"🔍 OpenSERP ({engine}): найдено {len(results)} результатов для {query!r}")
        return results

    @staticmethod
    def _parse_results(data: Dict[str, Any]) -> List[Dict[str, str]]:
        parsed: List[Dict[str, str]] = []
        for item in (data.get("results") or []):
            if not isinstance(item, dict):
                continue
            # Оставляем только органическую выдачу — реклама/related нам не нужны.
            item_type = item.get("type")
            if item_type and item_type != "organic":
                continue
            link = item.get("url") or item.get("link") or ""
            if not link:
                continue
            parsed.append({
                "title": item.get("title") or "",
                "link": link,
                "snippet": item.get("snippet") or "",
            })
        return parsed


# Глобальный экземпляр — переиспользуем сессию/конфиг, как lead_tools.
openserp_client = OpenSerpClient()
