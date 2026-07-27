"""
Клиент self-hosted OpenSERP (https://github.com/karust/openserp).

OpenSERP — бесплатный open-source SERP API. Основной источник поиска для
client_hunter (см. core/client_hunter_tools.py).

Важно: HTTP-таймаут клиента (OPENSERP_TIMEOUT_SEC) НЕ спасает от 504
«context deadline exceeded» — это таймаут ВНУТРИ OpenSERP (app.timeout).
См. openserp/config.yaml (timeout: 120) и retry/mega-fallback ниже.

Запуск с конфигом агентства:
    docker run --rm -p 127.0.0.1:7000:7000 \\
      -v \"%CD%/openserp/config.yaml:/config.yaml:ro\" \\
      karust/openserp:latest serve --config /config.yaml
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from core.config import Config

logger = logging.getLogger("OpenSerpClient")

# Временные ошибки OpenSERP (чек-лист устойчивости) — можно повторять.
_TRANSIENT_HTTP = frozenset({408, 429, 500, 502, 503, 504})


class OpenSerpClient:
    """HTTP-клиент OpenSERP с retry и резервными движками.

    Порядок для одного query:
      1. Основной engine (OPENSERP_ENGINE, обычно google) с retry+backoff
      2. /mega/search mode=any по fallback-движкам (если основной пуст/504)
      3. Поочерёдно dedicated endpoints fallback-движков
    Пустой список при полном провале — без исключений наружу.
    """

    def __init__(self, base_url: Optional[str] = None, timeout: Optional[int] = None) -> None:
        # None → Config; явный "" → не сконфигурирован (не подставлять дефолт из .env)
        if base_url is None:
            self.base_url = (Config.OPENSERP_BASE_URL or "").rstrip("/")
        else:
            self.base_url = (base_url or "").rstrip("/")
        self._timeout_override = timeout

    @property
    def timeout(self) -> int:
        if self._timeout_override is not None:
            return int(self._timeout_override)
        return int(Config.OPENSERP_TIMEOUT_SEC)

    @property
    def max_retries(self) -> int:
        return max(0, int(getattr(Config, "OPENSERP_MAX_RETRIES", 2)))

    @property
    def fallback_engines(self) -> List[str]:
        raw = getattr(Config, "OPENSERP_FALLBACK_ENGINES", "bing,yandex,duckduckgo") or ""
        primary = (Config.OPENSERP_ENGINE or "google").strip().lower()
        engines: List[str] = []
        for part in raw.split(","):
            name = part.strip().lower()
            if name and name != primary and name not in engines:
                engines.append(name)
        return engines

    def is_configured(self) -> bool:
        return bool(self.base_url)

    def search(
        self,
        query: str,
        engine: Optional[str] = None,
        limit: int = 10,
    ) -> List[Dict[str, str]]:
        """Ищет через OpenSERP. Возвращает [{title, link, snippet}]."""
        query = (query or "").strip()
        if not query or not self.is_configured():
            return []

        primary = (engine or Config.OPENSERP_ENGINE or "google").strip().lower()
        limit = max(1, min(limit, 100))

        # 1) Основной движок с retry
        results = self._search_engine_with_retries(query, primary, limit)
        if results:
            return results

        # 2) Mega any — первый ответивший из fallback (+ primary уже пробовали)
        mega = self._search_mega_any(query, [primary] + self.fallback_engines, limit)
        if mega:
            return mega

        # 3) Dedicated fallback engines по одному
        for alt in self.fallback_engines:
            results = self._search_engine_with_retries(query, alt, limit, retries=1)
            if results:
                logger.info(
                    f"🔍 OpenSERP fallback engine={alt}: {len(results)} результатов для {query!r}"
                )
                return results

        logger.warning(f"⚠️ OpenSERP: нет результатов для {query!r} (engine={primary}+fallback)")
        return []

    def _search_engine_with_retries(
        self,
        query: str,
        engine: str,
        limit: int,
        retries: Optional[int] = None,
    ) -> List[Dict[str, str]]:
        attempts = 1 + (self.max_retries if retries is None else max(0, retries))
        url = f"{self.base_url}/{engine}/search"
        params = {"text": query, "limit": limit}

        for attempt in range(1, attempts + 1):
            ok, results, retryable = self._http_get_results(url, params, query, engine)
            if ok and results:
                if attempt > 1:
                    logger.info(
                        f"🔍 OpenSERP ({engine}) успех на попытке {attempt}/{attempts}: "
                        f"{len(results)} результатов"
                    )
                else:
                    logger.info(
                        f"🔍 OpenSERP ({engine}): найдено {len(results)} результатов для {query!r}"
                    )
                return results
            if ok and not results:
                # 200, но пусто — не крутим retry (постоянный «нет выдачи»)
                return []
            if not retryable or attempt >= attempts:
                break
            delay = self._backoff_seconds(attempt)
            logger.warning(
                f"⚠️ OpenSERP ({engine}) попытка {attempt}/{attempts} неудачна для {query!r}; "
                f"повтор через {delay:.1f}с"
            )
            time.sleep(delay)
        return []

    def _search_mega_any(
        self,
        query: str,
        engines: List[str],
        limit: int,
    ) -> List[Dict[str, str]]:
        if not getattr(Config, "OPENSERP_USE_MEGA_FALLBACK", True):
            return []
        engines = [e for e in engines if e]
        if len(engines) < 2:
            return []
        url = f"{self.base_url}/mega/search"
        params = {
            "text": query,
            "limit": limit,
            "mode": "any",
            "engines": ",".join(engines),
        }
        ok, results, _ = self._http_get_results(url, params, query, "mega")
        if ok and results:
            logger.info(
                f"🔍 OpenSERP mega/any ({','.join(engines)}): "
                f"{len(results)} результатов для {query!r}"
            )
            return results
        return []

    def _http_get_results(
        self,
        url: str,
        params: Dict[str, Any],
        query: str,
        label: str,
    ) -> Tuple[bool, List[Dict[str, str]], bool]:
        """Returns (success_http_shape, results, retryable)."""
        try:
            response = requests.get(url, params=params, timeout=self.timeout)
        except requests.exceptions.Timeout as e:
            logger.warning(f"⚠️ OpenSERP timeout ({label}) {query!r}: {e}")
            return False, [], True
        except requests.exceptions.RequestException as e:
            logger.warning(f"⚠️ OpenSERP недоступен ({label}): {e}")
            return False, [], True

        if response.status_code in _TRANSIENT_HTTP:
            logger.warning(
                "⚠️ OpenSERP HTTP %s (%s) для %r: %s",
                response.status_code, label, query[:80], response.text[:200],
            )
            return False, [], True

        if response.status_code != 200:
            logger.warning(
                "⚠️ OpenSERP HTTP %s (%s) для %r: %s",
                response.status_code, label, query[:80], response.text[:200],
            )
            return False, [], False

        try:
            data = response.json()
        except ValueError as e:
            logger.warning(f"⚠️ OpenSERP невалидный JSON ({label}): {e}")
            return False, [], True

        return True, self._parse_results(data), False

    @staticmethod
    def _backoff_seconds(attempt: int) -> float:
        """Нарастающий интервал + jitter (чек-лист устойчивости)."""
        base = min(2 ** attempt, 30)
        return base + random.uniform(0, 1.5)

    @staticmethod
    def _parse_results(data: Dict[str, Any]) -> List[Dict[str, str]]:
        parsed: List[Dict[str, str]] = []
        for item in (data.get("results") or []):
            if not isinstance(item, dict):
                continue
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


openserp_client = OpenSerpClient()
