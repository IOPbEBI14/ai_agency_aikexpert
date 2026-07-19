"""Поиск лидов для lead_hunter через OpenSERP (те же правила, что client_hunter).

LLM не вызывает инструменты сам — TaskExecutor подмешивает google_search_results
до вызова модели. Лиды без привязки к SERP-результату отбрасываются.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from core.client_hunter_tools import (
    detect_icp,
    filter_prospect_hits,
    run_google_only_search,
)
from core.config import Config

logger = logging.getLogger("LeadHunter")

_MARKETPLACE_HOST_RE = re.compile(
    r"(?i)(^|\.)(wildberries\.ru|wb\.ru|ozon\.ru|avito\.ru|youla\.ru|"
    r"market\.yandex\.ru|aliexpress\.|amazon\.|dns-shop\.ru)$"
)

_SELLER_QUERY_TEMPLATES = [
    "бренд {category} официальный сайт магазин",
    "интернет-магазин {category} сайт компании контакты",
    "{category} производитель сайт опт розница",
    "магазин {category} купить официальный сайт -wildberries -ozon",
]


def _category_from_text(text: str) -> str:
    low = (text or "").lower()
    if "электроник" in low:
        return "электроника"
    if "косметик" in low:
        return "косметика"
    if "обув" in low:
        return "обувь"
    if "дом" in low or "уют" in low:
        return "товары для дома"
    if "одежд" in low:
        return "одежда"
    return "товары"


def build_lead_search_queries(
    goal: str = "",
    task_description: str = "",
    llm_queries: Optional[List[str]] = None,
    max_queries: int = 6,
) -> List[str]:
    """Запросы на сайты брендов/ИМ (не выдачу самого WB/Ozon)."""
    text = f"{task_description} {goal}".strip()
    category = _category_from_text(text)
    icp = detect_icp(goal=goal, task_description=task_description)

    queries: List[str] = []

    def _add(q: str) -> None:
        q = (q or "").strip()
        if q and q not in queries:
            queries.append(q)

    if llm_queries:
        for q in llm_queries:
            _add(str(q))

    for tmpl in _SELLER_QUERY_TEMPLATES:
        _add(tmpl.format(category=category))

    if icp.get("icp_key") == "seller":
        _add("бренд селлер сайт официальный магазин контакты")
        _add("интернет-магазин бренд каталог доставка сайт")

    result = queries[:max_queries]
    logger.info("lead_hunter queries (category=%s): %s", category, result)
    return result


def _is_marketplace_host(url: str) -> bool:
    try:
        host = urlparse(url or "").netloc.lower()
    except Exception:
        return False
    if host.startswith("www."):
        host = host[4:]
    return bool(_MARKETPLACE_HOST_RE.search(host)) or bool(
        _MARKETPLACE_HOST_RE.search("." + host)
    )


def filter_seller_hits(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Убирает SaaS/статьи и страницы самих маркетплейсов."""
    prospects, _noise = filter_prospect_hits(hits)
    out = []
    for h in prospects:
        link = (h.get("link") or "").strip()
        if not link or _is_marketplace_host(link):
            continue
        item = dict(h)
        item["hit_class"] = "prospect_candidate"
        out.append(item)
    return out


def run_lead_serp_search(
    goal: str = "",
    task_description: str = "",
    llm_queries: Optional[List[str]] = None,
    results_per_query: int = 5,
) -> Dict[str, Any]:
    """OpenSERP-поиск + фильтр → пакет для inject в lead_hunter."""
    queries = build_lead_search_queries(
        goal=goal,
        task_description=task_description,
        llm_queries=llm_queries,
    )
    raw = run_google_only_search(queries, results_per_query=results_per_query)
    prospects = filter_seller_hits(raw)
    icp = detect_icp(goal=goal, task_description=task_description)

    return {
        "google_search_results": prospects,
        "google_search_queries": queries,
        "google_prospect_candidates": prospects,
        "icp_detected": icp,
        "search_source_policy": (
            "ONLY_OPENSERP_GOOGLE — лиды только из google_search_results. "
            "Запрещено выдумывать компании, Telegram, email, телефоны."
        ),
    }


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def lead_matches_serp(lead: Dict[str, Any], serp: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Возвращает SERP-hit, если лид привязан к реальному результату поиска."""
    source_url = (lead.get("source_url") or lead.get("website") or "").strip()
    name = _norm(lead.get("company_name") or "")
    if source_url:
        for hit in serp:
            link = (hit.get("link") or "").strip()
            if link and (source_url.rstrip("/") == link.rstrip("/") or source_url in link or link in source_url):
                return hit
    if name and len(name) >= 3:
        for hit in serp:
            title = _norm(hit.get("title") or "")
            snippet = _norm(hit.get("snippet") or "")
            # имя компании должно быть заметно в title (не только общая ниша)
            if name in title or (len(name) >= 5 and name in snippet and name.split()[0] in title):
                return hit
    return None


def filter_hallucinated_leads(
    leads: List[Dict[str, Any]],
    serp: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Оставляет только лиды с привязкой к SERP; контакты без URL обнуляет."""
    kept: List[Dict[str, Any]] = []
    for lead in leads:
        if not isinstance(lead, dict):
            continue
        hit = lead_matches_serp(lead, serp)
        if not hit:
            logger.info(
                "lead_hunter: отброшен галлюцинированный лид %r",
                lead.get("company_name"),
            )
            continue
        item = dict(lead)
        item["website"] = item.get("website") or hit.get("link")
        item["source_url"] = item.get("source_url") or hit.get("link")
        item["source_query"] = item.get("source_query") or hit.get("query")
        item["source"] = item.get("source") or f"openserp:{hit.get('query') or 'google'}"
        # Контакты без подтверждения со страницы — не доверяем LLM
        if not item.get("website"):
            item["contact_email"] = None
            item["contact_phone"] = None
            item["contact_telegram"] = None
        kept.append(item)
    return kept


def enrich_lead_contacts(leads: List[Dict[str, Any]], max_scrapes: int = 10) -> List[Dict[str, Any]]:
    """Опциональный scrape сайта лида для реальных контактов."""
    if not getattr(Config, "CLIENT_HUNTER_SCRAPE_CONTACTS", True):
        return leads
    from core.outreach_export import enrich_clients_with_website_contacts

    as_clients = [
        {
            "company_name": L.get("company_name"),
            "website": L.get("website") or L.get("source_url"),
            "contact_email": L.get("contact_email"),
            "contact_phone": L.get("contact_phone"),
            "contact_telegram": L.get("contact_telegram"),
        }
        for L in leads
    ]
    enriched = enrich_clients_with_website_contacts(as_clients, max_scrapes=max_scrapes)
    out = []
    for lead, ec in zip(leads, enriched):
        item = dict(lead)
        item["contact_email"] = ec.get("contact_email")
        item["contact_phone"] = ec.get("contact_phone")
        item["contact_telegram"] = ec.get("contact_telegram")
        item["website"] = ec.get("website") or item.get("website")
        out.append(item)
    return out
