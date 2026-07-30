"""Декомпозиция tech_writer на сабтаски + сборка единого отчёта (Direction AL).

Промпт целиком перегружен (12 тем интеграции + FAQ + КП…) — одна LLM-задача
не тянет качество. Режем на фиксированные срезы, затем мержим в родителя.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .schemas import TechWriterResponse
from .task_ids import iteration_prefix_from_task_id

logger = logging.getLogger(__name__)

# Фиксированные срезы (без LLM-декомпозиции — предсказуемо и дёшево)
SLICE_SPECS: Dict[str, Dict[str, Any]] = {
    "overview": {
        "suffix": "tw_001",
        "title": "Документация: обзор интеграции (цель, источник, API, события, лимиты)",
        "focus": (
            "Секции: цель интеграции; источник и получатель; версия API; "
            "webhook/poll; постраничная выдача; ограничения API / лимиты. "
            "НЕ пиши контракт, критичные поля, ошибки, FAQ — это другие сабтаски."
        ),
        "required_groups": (
            ("цель", "задач"),
            ("источник", "получател"),
            ("api", "верси"),
            ("вебхук", "webhook", "опрос", "poll", "событи"),
            ("постранич", "пагинац", "страниц", "лимит", "ограничен"),
        ),
        "min_sections": 4,
        "min_faq": 0,
        "min_checklist": 0,
        "needs_guide": True,
    },
    "contract": {
        "suffix": "tw_002",
        "title": "Документация: контракт, критичные поля, ошибки, адаптер",
        "focus": (
            "Секции: контракт данных; критичные поля; обработка ошибок; "
            "адаптер / нормализация. Это КЛЮЧЕВЫЕ разделы — конкретика обязательна. "
            "НЕ дублируй обзор цели/источника и НЕ пиши FAQ целиком."
        ),
        "required_groups": (
            ("контракт",),
            ("критичн",),
            ("ошиб",),
            ("адаптер", "нормализац"),
        ),
        "min_sections": 4,
        "min_faq": 0,
        "min_checklist": 0,
        "needs_guide": True,
    },
    "ops": {
        "suffix": "tw_003",
        "title": "Документация: тестирование, сопровождение, FAQ, чек-лист",
        "focus": (
            "Секции: тестирование; сопровождение. Плюс faq[] (≥3) и checklist[] (≥5) "
            "по интеграции. Опционально короткий user_guide. "
            "НЕ переписывай контракт и критичные поля с нуля — опирайся на handoff."
        ),
        "required_groups": (
            ("тест",),
            ("сопровожд", "поддержк", "ответственн"),
        ),
        "min_sections": 2,
        "min_faq": 3,
        "min_checklist": 5,
        "needs_guide": True,
    },
}

SLICE_ORDER = ("overview", "contract", "ops")


def build_tech_writer_subtasks(
    parent_task: Dict[str, Any],
    *,
    parent_input: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Создаёт описания сабтасков tw_* с doc_slice и input_data."""
    parent_id = str(parent_task.get("task_id") or "")
    prefix = iteration_prefix_from_task_id(parent_id)
    base_input = dict(parent_input or {})
    # Сабтаски стартуют сразу после декомпозиции (зависимости родителя уже выполнены)
    subtasks: List[Dict[str, Any]] = []
    for slice_key in SLICE_ORDER:
        spec = SLICE_SPECS[slice_key]
        sub_id = f"{prefix}{spec['suffix']}"
        payload = {
            **base_input,
            "doc_slice": slice_key,
            "parent_task_id": parent_id,
            "slice_focus": spec["focus"],
            "required_section_hints": [
                "/".join(g) for g in spec["required_groups"]
            ],
        }
        subtasks.append({
            "task_id": sub_id,
            "agent_name": "tech_writer",
            "task_description": spec["title"],
            "doc_slice": slice_key,
            "depends_on": [],
            "input_data": payload,
        })
    return subtasks


def build_slice_system_prompt(base_prompt: str, doc_slice: str) -> str:
    """Узкий системный промпт для сабтаска: база + фокус среза."""
    spec = SLICE_SPECS.get(doc_slice) or {}
    focus = spec.get("focus") or doc_slice
    hints = ", ".join("/".join(g) for g in spec.get("required_groups") or ())
    min_sec = spec.get("min_sections", 1)
    min_faq = spec.get("min_faq", 0)
    min_cl = spec.get("min_checklist", 0)

    return f"""{base_prompt}

═══════════════════════════════════════════════════════════
САБТАСК / СРЕЗ ДОКУМЕНТАЦИИ (doc_slice={doc_slice})
═══════════════════════════════════════════════════════════

Сейчас ты выполняешь ТОЛЬКО этот срез. Не пытайся закрыть весь чек-лист из 12 пунктов.

ФОКУС:
{focus}

Требования среза:
- documents: хотя бы один integration_guide или tech_guide
- минимум {min_sec} секций по темам: {hints or "см. фокус"}
- faq: минимум {min_faq} (можно [] если 0)
- checklist: минимум {min_cl} (можно [] если 0)
- content секций ≥ 20 символов, конкретика из handoff (поля, коды, ноды)
- НЕ вкладывай весь JSON ответа внутрь content секции

Верни JSON той же формы (summary, documents, video_scripts, faq, checklist, notes),
но заполняй только свой срез. Пустые video_scripts допустимы.
"""


def _section_blob(doc: Dict[str, Any]) -> str:
    parts = [str(doc.get("title") or "")]
    for s in doc.get("sections") or []:
        if not isinstance(s, dict):
            continue
        parts.append(str(s.get("title") or ""))
        parts.append(str(s.get("content") or "")[:400])
    return " ".join(parts).lower()


def _group_covered(blob: str, group: Tuple[str, ...]) -> bool:
    return any(token in blob for token in group)


def validate_tech_writer_slice(
    data: Dict[str, Any], doc_slice: str
) -> Tuple[bool, List[str]]:
    """Лёгкая валидация среза (полная TechWriterResponse — только после merge)."""
    issues: List[str] = []
    spec = SLICE_SPECS.get(doc_slice)
    if not spec:
        return False, [f"Неизвестный doc_slice: {doc_slice}"]

    docs = [d for d in (data.get("documents") or []) if isinstance(d, dict)]
    guides = [
        d for d in docs
        if d.get("type") in ("integration_guide", "tech_guide")
    ]
    if spec.get("needs_guide") and not guides:
        issues.append("Нужен document type=integration_guide или tech_guide")

    primary = max(guides, key=lambda d: len(d.get("sections") or []), default=None)
    sections = (primary or {}).get("sections") or [] if primary else []
    if len(sections) < int(spec.get("min_sections") or 0):
        issues.append(
            f"Мало секций в guide: {len(sections)} < {spec.get('min_sections')}"
        )

    for s in sections:
        if not isinstance(s, dict):
            continue
        body = str(s.get("content") or "").strip()
        if len(body) < 20:
            issues.append(f"Короткая секция «{s.get('title')}»")
        if body.startswith("{") and '"sections"' in body[:200]:
            issues.append(f"Секция «{s.get('title')}» содержит вложенный JSON")

    if primary:
        blob = _section_blob(primary)
        missing = [
            "/".join(g)
            for g in spec.get("required_groups") or ()
            if not _group_covered(blob, g)
        ]
        if missing:
            issues.append("Не покрыты темы среза: " + ", ".join(missing))

    faq = data.get("faq") or []
    if len(faq) < int(spec.get("min_faq") or 0):
        issues.append(f"faq: нужно ≥{spec['min_faq']}, сейчас {len(faq)}")

    checklist = [c for c in (data.get("checklist") or []) if str(c).strip()]
    if len(checklist) < int(spec.get("min_checklist") or 0):
        issues.append(
            f"checklist: нужно ≥{spec['min_checklist']}, сейчас {len(checklist)}"
        )

    return (not issues), issues


def _parse_output(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def merge_tech_writer_slices(
    slice_outputs: Sequence[Dict[str, Any]],
    *,
    slice_keys: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Собирает единый TechWriterResponse dict из выходов сабтасков."""
    all_sections: List[Dict[str, Any]] = []
    seen_titles: set[str] = set()
    extra_docs: List[Dict[str, Any]] = []
    faq: List[Dict[str, Any]] = []
    checklist: List[str] = []
    video_scripts: List[Dict[str, Any]] = []
    summaries: List[str] = []
    guide_title = "Документация интеграции"
    guide_audience = "Администратор интеграции / сопровождение"

    ordered = list(slice_outputs)
    if slice_keys:
        # сохраняем порядок overview → contract → ops если ключи переданы снаружи
        pass

    for out in ordered:
        data = _parse_output(out)
        if data.get("summary"):
            summaries.append(str(data["summary"]).strip())
        for doc in data.get("documents") or []:
            if not isinstance(doc, dict):
                continue
            dtype = doc.get("type")
            if dtype in ("integration_guide", "tech_guide"):
                if doc.get("title"):
                    guide_title = str(doc["title"])
                if doc.get("audience"):
                    guide_audience = str(doc["audience"])
                for s in doc.get("sections") or []:
                    if not isinstance(s, dict):
                        continue
                    key = re.sub(r"\s+", " ", str(s.get("title") or "").strip().lower())
                    if not key or key in seen_titles:
                        continue
                    seen_titles.add(key)
                    all_sections.append(s)
            else:
                extra_docs.append(doc)
        for item in data.get("faq") or []:
            if isinstance(item, dict) and item.get("question"):
                faq.append(item)
        for c in data.get("checklist") or []:
            text = str(c).strip()
            if text and text not in checklist:
                checklist.append(text)
        for vs in data.get("video_scripts") or []:
            if isinstance(vs, dict):
                video_scripts.append(vs)

    summary = (
        "Собрана документация из сабтасков tech_writer: "
        + "; ".join(s for s in summaries if s)[:500]
    )
    if len(summary) < 10:
        summary = "Собрана документация интеграции из подзадач tech_writer"

    merged = {
        "summary": summary,
        "documents": [
            {
                "title": guide_title,
                "type": "integration_guide",
                "audience": guide_audience,
                "sections": all_sections,
            },
            *extra_docs,
        ],
        "video_scripts": video_scripts,
        "faq": faq,
        "checklist": checklist,
        "notes": "merged_from_tech_writer_subtasks",
    }
    # Строгая валидация итога (Direction W)
    validated = TechWriterResponse(**merged)
    return validated.model_dump()


def parse_subtask_output(task: Dict[str, Any]) -> Dict[str, Any]:
    return _parse_output(task.get("output_data"))
