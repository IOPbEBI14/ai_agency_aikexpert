"""Поиск клиентов для монетизации: только Google-канал (открытые источники).

Источники (приоритет — чек-лист устойчивости, п.5 «резервный сценарий»):
1. OpenSERP (core.openserp_client) — self-hosted, бесплатный, основной источник.
2. Google Custom Search API (core.lead_tools) — платный резерв, если OpenSERP
   недоступен/пуст И заданы GOOGLE_API_KEY/GOOGLE_CX.
Оба источника бьют по Google → политика «только Google» не нарушается.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

from core.config import Config
from core.lead_tools import lead_tools
from core.openserp_client import openserp_client

logger = logging.getLogger("ClientHunter")

# Запросы по умолчанию, если LLM/задача не дали своих
_DEFAULT_QUERY_TEMPLATES = [
    "{niche} автоматизация бизнес процессов",
    "{niche} CRM заявки мессенджеры",
    "{niche} интеграция n8n OR albato",
]


def build_search_queries(
    goal: str = "",
    task_description: str = "",
    llm_queries: Optional[List[str]] = None,
    max_queries: int = 4,
) -> List[str]:
    """Собирает список Google-запросов из ответа LLM / задачи / цели проекта."""
    queries: List[str] = []
    if llm_queries:
        for q in llm_queries:
            q = (q or "").strip()
            if q and q not in queries:
                queries.append(q)

    text = f"{task_description} {goal}".strip()
    # Простые эвристики ниши из goal
    niche = "e-commerce селлер"
    low = text.lower()
    if "школ" in low:
        niche = "школа онлайн образование"
    elif "клиник" in low or "мед" in low:
        niche = "клиника медицинский центр"
    elif "ресторан" in low or "кафе" in low:
        niche = "ресторан кафе"
    elif "wb" in low or "wildberries" in low or "ozon" in low or "селлер" in low:
        niche = "селлер wildberries ozon"
    elif "магазин" in low:
        niche = "интернет магазин"

    for tmpl in _DEFAULT_QUERY_TEMPLATES:
        q = tmpl.format(niche=niche)
        if q not in queries:
            queries.append(q)

    return queries[:max_queries]


def run_google_only_search(
    queries: List[str],
    results_per_query: int = 5,
) -> List[Dict[str, Any]]:
    """Выполняет поиск ТОЛЬКО через Google-канал (открытые источники).

    Чек-лист устойчивости:
    - Тип ошибки: недоступность OpenSERP/Google API — временная, не валит процесс.
    - Резервный сценарий: OpenSERP не ответил/пусто по запросу → пробуем Google
      Custom Search API для ЭТОГО ЖЕ запроса (если настроены ключи).
    - Если оба источника пусты для всех запросов — честно возвращаем [] и логируем
      предупреждение; client_hunter_prompt.txt требует не выдумывать клиентов в этом случае.
    """
    collected: List[Dict[str, Any]] = []
    seen_links = set()

    def _collect(hits: List[Dict[str, Any]], query: str) -> None:
        for hit in hits:
            link = (hit.get("link") or "").strip()
            if not link or link in seen_links:
                continue
            seen_links.add(link)
            collected.append({
                "title": hit.get("title") or "",
                "link": link,
                "snippet": hit.get("snippet") or "",
                "query": query,
                "source": "google",
            })

    google_api_configured = bool(Config.GOOGLE_API_KEY and Config.GOOGLE_CX)

    for query in queries:
        hits: List[Dict[str, Any]] = []
        try:
            hits = openserp_client.search(query, engine=Config.OPENSERP_ENGINE, limit=results_per_query)
        except Exception as e:
            logger.error(f"OpenSERP search failed for {query!r}: {e}")

        if hits:
            _collect(hits, query)
            continue

        # Резерв: OpenSERP недоступен/пуст для этого запроса → платный Google API.
        if google_api_configured:
            try:
                hits = lead_tools.search_google(query, num_results=results_per_query)
            except Exception as e:
                logger.error(f"Google API search failed for {query!r}: {e}")
                hits = []
            _collect(hits, query)

    if not collected:
        logger.warning(
            "client_hunter: ни OpenSERP (%s), ни Google Custom Search API "
            "не вернули результатов ни по одному из %d запросов",
            Config.OPENSERP_BASE_URL, len(queries),
        )
    logger.info(f"client_hunter Google: {len(collected)} уникальных результатов")
    return collected


def company_name_from_title(title: str) -> str:
    """Грубая очистка title Google → имя компании."""
    name = (title or "").strip()
    name = re.split(r"\s+[|\-–—:]\s+", name)[0]
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120] or "Неизвестная компания"
