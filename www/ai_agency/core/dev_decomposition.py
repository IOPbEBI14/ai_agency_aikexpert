"""Нормализация декомпозиции developer после architect.

Проблема (Direction V): PM режет один сценарий на шаги (retry, ошибки, журнал),
а каждая dev_* получает полный blueprint и контракт «готовый n8n JSON» →
несколько почти одинаковых workflow.

Правило: при наличии workflow_blueprint ровно одна задача с
artifact_mode=full_workflow; остальные — prep/spec без n8n_json.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional

logger = logging.getLogger("DevDecomposition")

MODE_FULL = "full_workflow"
MODE_SPEC = "spec"
MODE_PREP = "prep"
VALID_MODES = frozenset({MODE_FULL, MODE_SPEC, MODE_PREP})

_PREP_HINT = re.compile(
    r"(таблиц|bpium|nocodb|credentials?|переменн|env\b|секрет|webhook.?secret|"
    r"настроить\s+окружен|создать\s+таблиц)",
    re.IGNORECASE,
)
_WORKFLOW_HINT = re.compile(
    r"(workflow|n8n|нод|retry|backoff|дубл|идемпотент|webhook\s+event|"
    r"цикл\s+повтор|error.?ветк|обработк\w*\s+ошиб|сериализ)",
    re.IGNORECASE,
)


def _as_dict(subtask: Any) -> Dict[str, Any]:
    if hasattr(subtask, "model_dump"):
        return subtask.model_dump()
    if isinstance(subtask, dict):
        return dict(subtask)
    raise TypeError(f"Подзадача должна быть dict/BaseModel, получено {type(subtask)}")


def _guess_mode(description: str, explicit: str) -> str:
    if explicit in VALID_MODES:
        return explicit
    text = description or ""
    if _PREP_HINT.search(text) and not _WORKFLOW_HINT.search(text):
        return MODE_PREP
    return ""  # undecided → later collapse / promote


def _topo_last(subtasks: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Подзадача с наибольшим числом зависимостей (или последняя в списке)."""
    return max(
        subtasks,
        key=lambda s: (len(s.get("depends_on") or []), subtasks.index(s)),
    )


def _collapse_workflow_slice(
    slices: List[Dict[str, Any]],
    *,
    keep_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Склеить фиче-срезы одного workflow в одну full_workflow задачу."""
    sid = keep_id or "dev_001"
    lines = []
    contexts = []
    for s in slices:
        lines.append(f"- {s.get('subtask_id')}: {s.get('description', '').strip()}")
        ctx = (s.get("context") or "").strip()
        if ctx:
            contexts.append(f"[{s.get('subtask_id')}] {ctx}")
    return {
        "subtask_id": sid,
        "description": (
            "Реализовать ЕДИНЫЙ импортируемый n8n workflow по workflow_blueprint "
            "архитектора (один JSON на весь сценарий). Включить ВСЕ требования ниже — "
            "не создавать отдельные workflow на каждый пункт:\n" + "\n".join(lines)
        ),
        "depends_on": [],
        "context": "\n".join(contexts)[:6000],
        "artifact_mode": MODE_FULL,
        "assigned_node_names": [],
    }


def normalize_developer_subtasks(
    subtasks: List[Any],
    *,
    has_blueprint: bool,
) -> List[Dict[str, Any]]:
    """Гарантирует не более одного full_workflow при наличии blueprint."""
    raw = [_as_dict(s) for s in subtasks]
    if not raw:
        return raw

    normalized: List[Dict[str, Any]] = []
    for s in raw:
        sid = (s.get("subtask_id") or "").strip() or "dev_001"
        desc = (s.get("description") or "").strip()
        explicit = (s.get("artifact_mode") or "").strip().lower()
        mode = _guess_mode(desc, explicit)
        item = {
            "subtask_id": sid,
            "description": desc or sid,
            "depends_on": list(s.get("depends_on") or []),
            "context": s.get("context") or "",
            "artifact_mode": mode,
            "assigned_node_names": list(s.get("assigned_node_names") or []),
        }
        normalized.append(item)

    if not has_blueprint:
        # Без blueprint — оставляем как есть; пустой mode → full (историческое поведение)
        for s in normalized:
            if not s["artifact_mode"]:
                s["artifact_mode"] = MODE_FULL
        return _ensure_unique_ids(normalized)

    prep = [s for s in normalized if s["artifact_mode"] == MODE_PREP]
    specs = [s for s in normalized if s["artifact_mode"] == MODE_SPEC]
    fulls = [s for s in normalized if s["artifact_mode"] == MODE_FULL]
    undecided = [s for s in normalized if not s["artifact_mode"]]

    # Неразмеченные + лишние full → один owner (типичный баг: 4 фиче-среза)
    workflow_candidates = fulls + undecided
    if len(workflow_candidates) > 1:
        logger.warning(
            "⚠️ Декомпозиция: %s кандидатов на n8n workflow при одном blueprint — "
            "склеиваем в одну full_workflow (prep/spec сохраняем)",
            len(workflow_candidates),
        )
        owner = _collapse_workflow_slice(workflow_candidates)
        result = prep + specs + [owner]
        # prep зависят от ничего; owner зависит от prep
        if prep:
            owner["depends_on"] = [p["subtask_id"] for p in prep]
        return _ensure_unique_ids(result)

    if len(workflow_candidates) == 1:
        workflow_candidates[0]["artifact_mode"] = MODE_FULL
        return _ensure_unique_ids(prep + specs + workflow_candidates)

    # Только prep/spec без full — повышаем последнюю non-prep до full, иначе синтетика
    if specs:
        owner = dict(_topo_last(specs))
        owner["artifact_mode"] = MODE_FULL
        rest = [s for s in specs if s["subtask_id"] != owner["subtask_id"]]
        for s in rest:
            s["artifact_mode"] = MODE_SPEC
        logger.warning(
            "⚠️ Нет full_workflow — назначаем owner: %s", owner["subtask_id"]
        )
        return _ensure_unique_ids(prep + rest + [owner])

    if prep:
        # Только подготовка — добавляем одну задачу сборки workflow
        owner = {
            "subtask_id": "dev_workflow",
            "description": (
                "Собрать ЕДИНЫЙ импортируемый n8n workflow по workflow_blueprint архитектора"
            ),
            "depends_on": [p["subtask_id"] for p in prep],
            "context": "Source of truth — workflow_blueprint; учти результаты prep-задач",
            "artifact_mode": MODE_FULL,
            "assigned_node_names": [],
        }
        logger.info("➕ Добавлена full_workflow после prep: dev_workflow")
        return _ensure_unique_ids(prep + [owner])

    return _ensure_unique_ids(normalized)


def _ensure_unique_ids(subtasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    for i, s in enumerate(subtasks):
        sid = s["subtask_id"]
        if sid in seen:
            s["subtask_id"] = f"{sid}_{i + 1}"
        seen.add(s["subtask_id"])
    return subtasks


def build_developer_input_data(
    *,
    subtask: Dict[str, Any],
    architecture_summary: str,
    blueprint: Any,
    blueprint_str: str,
) -> Dict[str, Any]:
    """input_data для create_task: полный blueprint только у full_workflow."""
    mode = subtask.get("artifact_mode") or MODE_FULL
    payload: Dict[str, Any] = {
        "context": subtask.get("context") or "",
        "architecture_summary": (architecture_summary or "")[:2000],
        "artifact_mode": mode,
        "assigned_node_names": subtask.get("assigned_node_names") or [],
    }
    if mode == MODE_FULL:
        payload["workflow_blueprint"] = blueprint_str
    elif mode == MODE_SPEC and blueprint_str:
        # Узкий фрагмент: только имена нод, если указаны
        names = set(subtask.get("assigned_node_names") or [])
        if names and isinstance(blueprint, dict):
            wb = blueprint.get("workflow_blueprint") or blueprint
            nodes = []
            if isinstance(wb, dict):
                nodes = [
                    n for n in (wb.get("nodes") or [])
                    if isinstance(n, dict) and n.get("name") in names
                ]
            payload["workflow_blueprint_fragment"] = {
                "nodes": nodes,
                "note": "Спецификация нод; НЕ сериализуй полный n8n_json",
            }
        payload["expect_n8n_json"] = False
    else:
        payload["expect_n8n_json"] = False
    return payload


def strip_n8n_from_spec_response(response_data: Dict[str, Any]) -> Dict[str, Any]:
    """Убрать n8n_json и n8n_workflow-файлы у non-full ответов."""
    data = dict(response_data)
    data["n8n_json"] = None
    data["workflow_name"] = None
    files = []
    for f in data.get("files") or []:
        if isinstance(f, dict) and f.get("type") == "n8n_workflow":
            continue
        files.append(f)
    if not files:
        files = [{
            "name": "spec-notes.md",
            "type": "config",
            "description": "Спецификация / prep без отдельного workflow",
        }]
    data["files"] = files
    return data
