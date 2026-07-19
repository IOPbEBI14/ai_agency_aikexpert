"""Поиск клиентов для монетизации: только Google-канал (открытые источники).

Источники (приоритет — чек-лист устойчивости, п.5 «резервный сценарий»):
1. OpenSERP (core.openserp_client) — self-hosted, бесплатный, основной источник.
2. Google Custom Search API (core.lead_tools) — платный резерв, если OpenSERP
   недоступен/пуст И заданы GOOGLE_API_KEY/GOOGLE_CX.
Оба источника бьют по Google → политика «только Google» не нарушается.

Стратегия запросов (ICP → сайты бизнесов, не SaaS/статьи):
запросы вида «{ниша} автоматизация CRM» находят вендоров и обзоры;
client_hunter ищет ЛИДОВ — частные клиники, магазины и т.п.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from core.config import Config
from core.lead_tools import lead_tools
from core.openserp_client import openserp_client

logger = logging.getLogger("ClientHunter")

# Крупные города РФ — для гео-уточнения запросов из goal/ТЗ
_GEO_TOKENS = (
    "москва", "ижевск", "московская", "спб", "петербург", "санкт-петербург", "ленинградская",
    "новосибирск", "екатеринбург", "казань", "нижний новгород", "челябинск",
    "самара", "омск", "ростов", "уфа", "красноярск", "воронеж", "пермь",
    "волгоград", "краснодар", "саратов", "тюмень", "тольятти", "ижевск",
    "барнаул", "иркутск", "хабаровск", "ярославль", "владивосток", "махачкала",
    "томск", "оренбург", "кемерово", "новокузнецк", "рязань", "астрахань",
    "пенза", "липецк", "тула", "киров", "чебоксары", "калининград", "брянск",
    "курск", "иваново", "магнитогорск", "тверь", "ставрополь", "ульяновск",
    "белгород", "сочи",
)

# Признаки выдачи про продукты/статьи, а не про компанию-клиента
_VENDOR_NOISE_RE = re.compile(
    r"(?i)\b("
    r"crm|amocrm|amo\.crm|bitrix|битрикс|yclients|юклиент|albato|n8n|"
    r"автоматизац\w*|интеграц\w*|внедрен\w*|no-?code|low-?code|"
    r"saas|api\b|webhook|хабр|habr|vc\.ru|medium\.com|"
    r"как\s+(выбрать|внедрить|настроить)|топ[- ]?\d+|обзор\s+систем|"
    r"программ\w*\s+для\s+(клиник|стоматолог)|рейтинг\s+crm"
    r")\b"
)

_VENDOR_HOST_RE = re.compile(
    r"(?i)("
    r"yclients\.|amocrm\.|bitrix24\.|albato\.|n8n\.io|habr\.com|vc\.ru|"
    r"wikipedia\.|youtube\.|vk\.com/wall"
    r")"
)

# Шаблоны: ищем САЙТЫ целевых бизнесов (лиды), не продукты автоматизации
_PROSPECT_TEMPLATES: Dict[str, List[str]] = {
    "dental": [
        "частная стоматология {geo} официальный сайт",
        "стоматологическая клиника {geo} записаться на приём",
        'стоматология {geo} "главный врач"',
        "стоматология {geo} отзывы пациентов сайт",
    ],
    "clinic": [
        "частная клиника {geo} официальный сайт",
        "медицинский центр {geo} записаться",
        'клиника {geo} "главный врач" OR собственник',
        "частная поликлиника {geo} сайт",
    ],
    "school": [
        "онлайн школа {geo} официальный сайт",
        "курсы {geo} запись сайт школы",
        "образовательный центр {geo} обучение",
    ],
    "restaurant": [
        "ресторан {geo} официальный сайт меню",
        "кафе {geo} доставка сайт",
        "сеть ресторанов {geo} бронирование",
    ],
    "seller": [
        "бренд одежды официальный сайт интернет-магазин",
        "интернет-магазин бренд каталог доставка сайт компании",
        "производитель товаров сайт опт розница контакты",
        "магазин бренд купить официальный сайт отзывы",
    ],
    "ecommerce": [
        "интернет-магазин {geo} официальный сайт",
        "онлайн магазин {geo} доставка каталог",
    ],
    "generic": [
        "{niche} {geo} официальный сайт",
        "{niche} {geo} контакты компания",
        '"{niche}" {geo} отзывы клиентов сайт',
    ],
}

# Устаревшие «vendor-seeking» шаблоны — только как слабый доп. хвост, не primary
_LEGACY_AUTOMATION_TEMPLATES = [
    "{niche} автоматизация бизнес процессов",
    "{niche} CRM заявки мессенджеры",
]


def _extract_geo(text: str) -> str:
    low = (text or "").lower()
    found: List[str] = []
    for token in _GEO_TOKENS:
        if token in low and token not in found:
            # нормализуем короткие алиасы
            if token in ("спб", "петербург", "санкт-петербург"):
                label = "Санкт-Петербург"
            elif token in ("москва", "московская"):
                label = "Москва"
            else:
                label = token.title()
            if label not in found:
                found.append(label)
            if len(found) >= 2:
                break
    return " ".join(found) if found else "Россия"


def detect_icp(goal: str = "", task_description: str = "") -> Dict[str, str]:
    """Определяет тип ICP и базовую нишу из цели/ТЗ задачи."""
    text = f"{task_description} {goal}".strip()
    low = text.lower()
    geo = _extract_geo(text)

    if any(k in low for k in ("стоматолог", "зубоврач", "дентал", "dental")):
        return {
            "icp_key": "dental",
            "niche": "частная стоматология",
            "geo": geo,
            "label": "частная стоматологическая клиника",
        }
    if any(k in low for k in ("клиник", "поликлиник", "медцентр", "медицинск", "мед ")):
        return {
            "icp_key": "clinic",
            "niche": "частная клиника",
            "geo": geo,
            "label": "частная медицинская клиника / медцентр",
        }
    if "школ" in low or "образован" in low or "курс" in low:
        return {
            "icp_key": "school",
            "niche": "онлайн школа",
            "geo": geo,
            "label": "онлайн-школа / образовательный центр",
        }
    if any(k in low for k in ("ресторан", "кафе", "общепит", "horeca")):
        return {
            "icp_key": "restaurant",
            "niche": "ресторан кафе",
            "geo": geo,
            "label": "ресторан / кафе",
        }
    if any(k in low for k in ("wb", "wildberries", "ozon", "селлер", "маркетплейс")):
        return {
            "icp_key": "seller",
            "niche": "селлер wildberries ozon",
            "geo": geo,
            "label": "селлер маркетплейса",
        }
    if any(k in low for k in ("магазин", "e-commerce", "ecommerce", "интернет-магазин")):
        return {
            "icp_key": "ecommerce",
            "niche": "интернет магазин",
            "geo": geo,
            "label": "интернет-магазин",
        }
    # fallback: вытащить короткую фразу из goal
    niche = re.sub(r"\s+", " ", text)[:60].strip() or "компания"
    return {
        "icp_key": "generic",
        "niche": niche,
        "geo": geo,
        "label": niche,
    }


def looks_like_vendor_or_article(hit: Dict[str, Any]) -> bool:
    """True, если результат похож на SaaS/статью/обзор, а не на сайт целевого бизнеса."""
    blob = " ".join(
        [
            str(hit.get("title") or ""),
            str(hit.get("snippet") or ""),
            str(hit.get("link") or ""),
        ]
    )
    if _VENDOR_HOST_RE.search(blob):
        return True
    return bool(_VENDOR_NOISE_RE.search(blob))


def filter_prospect_hits(
    hits: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Разделяет выдачу на кандидатов-лидов и шум (вендоры/статьи)."""
    prospects: List[Dict[str, Any]] = []
    noise: List[Dict[str, Any]] = []
    for hit in hits:
        if looks_like_vendor_or_article(hit):
            noise.append(hit)
        else:
            prospects.append(hit)
    return prospects, noise


def _is_prospect_oriented_query(query: str) -> bool:
    """Запросы про автоматизацию/CRM почти всегда тянут вендоров — понижаем приоритет."""
    q = (query or "").lower()
    if not q.strip():
        return False
    bad = (
        "автоматизац", "crm", "albato", "n8n", "интеграц", "внедрен",
        "bitrix", "amocrm", "no-code", "low-code",
    )
    good = (
        "официальный сайт", "записаться", "отзывы", "главный врач",
        "контакты", "клиника", "стоматолог", "магазин", "ресторан",
        "школа", "селлер", "бренд",
    )
    if any(b in q for b in bad) and not any(g in q for g in good):
        return False
    return True


def build_search_queries(
    goal: str = "",
    task_description: str = "",
    llm_queries: Optional[List[str]] = None,
    max_queries: int = 6,
) -> List[str]:
    """Собирает Google-запросы: сначала ICP-проспектные, затем валидные из LLM/ТЗ."""
    icp = detect_icp(goal=goal, task_description=task_description)
    geo = icp["geo"]
    niche = icp["niche"]
    templates = _PROSPECT_TEMPLATES.get(icp["icp_key"], _PROSPECT_TEMPLATES["generic"])

    queries: List[str] = []

    def _add(q: str) -> None:
        q = (q or "").strip()
        if q and q not in queries:
            queries.append(q)

    # 1) ICP-шаблоны (основной канал — сайты бизнесов)
    for tmpl in templates:
        _add(tmpl.format(geo=geo, niche=niche).strip())

    # 2) Явные запросы из input_data / LLM — только prospect-oriented первыми
    if llm_queries:
        oriented = [q for q in llm_queries if _is_prospect_oriented_query(str(q))]
        other = [q for q in llm_queries if str(q).strip() and q not in oriented]
        for q in oriented + other:
            _add(str(q))

    # 3) Слабый хвост (legacy) — только если ещё есть слоты и нет dental/clinic ICP
    # Для dental/clinic legacy почти всегда вреден (даёт SaaS).
    if icp["icp_key"] not in ("dental", "clinic") and len(queries) < max_queries:
        for tmpl in _LEGACY_AUTOMATION_TEMPLATES:
            _add(tmpl.format(niche=niche))

    result = queries[:max_queries]
    logger.info(
        "client_hunter queries (icp=%s geo=%s): %s",
        icp["icp_key"], geo, result,
    )
    return result


def build_refined_queries(
    goal: str = "",
    task_description: str = "",
    max_queries: int = 4,
) -> List[str]:
    """Второй проход: более «локальные» запросы, если первый дал в основном шум."""
    icp = detect_icp(goal=goal, task_description=task_description)
    geo = icp["geo"]
    if geo == "Россия":
        # без города — пробуем топ-рынки
        geos = ["Москва", "Санкт-Петербург", "Екатеринбург", "Казань"]
    else:
        geos = [g for g in geo.split() if g][:2] or [geo]

    refined: List[str] = []
    if icp["icp_key"] == "dental":
        patterns = [
            "частная стоматология {g} сайт клиники",
            "стоматология {g} имплантация записаться",
            '"{g}" стоматология телефон адрес',
        ]
    elif icp["icp_key"] == "clinic":
        patterns = [
            "частная клиника {g} официальный сайт",
            "медицинский центр {g} запись к врачу",
            'клиника {g} "о нас" сайт',
        ]
    else:
        patterns = [
            "{niche} {g} официальный сайт".replace("{niche}", icp["niche"]),
            "{niche} {g} контакты".replace("{niche}", icp["niche"]),
        ]

    for g in geos:
        for p in patterns:
            q = p.format(g=g).strip()
            if q and q not in refined:
                refined.append(q)
            if len(refined) >= max_queries:
                return refined
    return refined[:max_queries]


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
            hits = openserp_client.search(
                query, engine=Config.OPENSERP_ENGINE, limit=results_per_query
            )
        except Exception as e:
            logger.error(f"OpenSERP search failed for {query!r}: {e}")

        if hits:
            _collect(hits, query)
            continue

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


def run_icp_search(
    goal: str = "",
    task_description: str = "",
    llm_queries: Optional[List[str]] = None,
    results_per_query: int = 5,
) -> Dict[str, Any]:
    """Полный цикл: ICP-запросы → поиск → фильтр шума → при необходимости 2-й проход.

    Возвращает dict для inject в input_data LLM.
    """
    queries = build_search_queries(
        goal=goal,
        task_description=task_description,
        llm_queries=llm_queries,
    )
    raw = run_google_only_search(queries, results_per_query=results_per_query)
    prospects, noise = filter_prospect_hits(raw)

    refined_used: List[str] = []
    if len(prospects) < 2:
        refined_used = build_refined_queries(goal=goal, task_description=task_description)
        # не повторяем уже выполненные
        refined_used = [q for q in refined_used if q not in queries]
        if refined_used:
            logger.info(
                "client_hunter: мало prospect-хитов (%d), второй проход: %s",
                len(prospects), refined_used,
            )
            raw2 = run_google_only_search(refined_used, results_per_query=results_per_query)
            # merge
            seen = {r.get("link") for r in raw}
            for hit in raw2:
                if hit.get("link") not in seen:
                    raw.append(hit)
                    seen.add(hit.get("link"))
            prospects, noise = filter_prospect_hits(raw)

    # В LLM отдаём сначала кандидатов, потом шум (с пометкой) — чтобы модель фильтровала явно
    tagged: List[Dict[str, Any]] = []
    for hit in prospects:
        item = dict(hit)
        item["hit_class"] = "prospect_candidate"
        tagged.append(item)
    for hit in noise:
        item = dict(hit)
        item["hit_class"] = "vendor_or_article"
        tagged.append(item)

    icp = detect_icp(goal=goal, task_description=task_description)
    return {
        "google_search_results": tagged,
        "google_search_queries": queries + refined_used,
        "google_prospect_candidates": prospects,
        "google_noise_count": len(noise),
        "icp_detected": icp,
        "search_source_policy": (
            "ONLY_GOOGLE_OPEN_SOURCES — запрещены Telegram, Avito, scrape, закрытые базы. "
            "Ищем сайты целевых бизнесов (ICP), не SaaS/статьи про автоматизацию."
        ),
    }


def company_name_from_title(title: str) -> str:
    """Грубая очистка title Google → имя компании."""
    name = (title or "").strip()
    name = re.split(r"\s+[|\-–—:]\s+", name)[0]
    name = re.sub(r"\s+", " ", name).strip()
    return name[:120] or "Неизвестная компания"
