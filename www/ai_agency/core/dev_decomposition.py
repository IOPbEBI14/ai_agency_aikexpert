"""Нормализация декомпозиции developer после architect.

Direction V: при ОДНОМ blueprint — не больше одной full_workflow
(фиче-срезы retry/ошибки/журнал склеиваются).

Direction AE: при НЕСКОЛЬКИХ независимых workflow (workflow_blueprints[]
или N разных сценариев в цели/архитектуре) — РОВНО одна full_workflow
задача на каждый workflow. В одном n8n_json нельзя делать два сценария.
"""
from __future__ import annotations

import json
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
            "не создавать отдельные workflow на каждый пункт и не объединять "
            "несколько независимых сценариев:\n" + "\n".join(lines)
        ),
        "depends_on": [],
        "context": "\n".join(contexts)[:6000],
        "artifact_mode": MODE_FULL,
        "assigned_node_names": [],
        "workflow_id": "wf_1",
    }


def extract_workflow_units(handoff: Any) -> List[Dict[str, Any]]:
    """Достаёт список независимых workflow из handoff_to_developer.

    Returns:
        [{workflow_id, name, blueprint}, ...]
    """
    if not handoff:
        return []
    if hasattr(handoff, "model_dump"):
        handoff = handoff.model_dump()
    if not isinstance(handoff, dict):
        return []

    units: List[Dict[str, Any]] = []
    blueprints = handoff.get("workflow_blueprints")
    if isinstance(blueprints, list) and blueprints:
        for i, bp in enumerate(blueprints):
            if not isinstance(bp, dict):
                continue
            if "nodes" in bp or "connections" in bp:
                inner = bp
                name = str(bp.get("name") or bp.get("workflow_name") or f"workflow_{i + 1}")
                wid = str(bp.get("workflow_id") or f"wf_{i + 1}")
            else:
                inner = bp.get("workflow_blueprint") or {}
                if not isinstance(inner, dict):
                    inner = {}
                name = str(bp.get("name") or bp.get("workflow_name") or f"workflow_{i + 1}")
                wid = str(bp.get("workflow_id") or f"wf_{i + 1}")
            if not (inner.get("nodes") or inner.get("connections")):
                # Именованный слот без нод — всё равно отдельный workflow для декомпозиции
                if not (name or wid):
                    continue
            units.append({"workflow_id": wid, "name": name, "blueprint": inner if (inner.get("nodes") or inner.get("connections")) else bp})
        if units:
            return units

    single = handoff.get("workflow_blueprint")
    if isinstance(single, dict) and (single.get("nodes") or single.get("connections")):
        return [{
            "workflow_id": str(single.get("workflow_id") or "wf_1"),
            "name": str(
                single.get("name")
                or handoff.get("workflow_name")
                or "main"
            ),
            "blueprint": single,
        }]

    if handoff.get("nodes"):
        return [{
            "workflow_id": "wf_1",
            "name": str(handoff.get("name") or "main"),
            "blueprint": handoff,
        }]
    return []


def _match_unit_to_subtask(
    unit: Dict[str, Any],
    candidates: List[Dict[str, Any]],
    used: set,
) -> Optional[Dict[str, Any]]:
    wid = (unit.get("workflow_id") or "").lower()
    name = (unit.get("name") or "").lower()
    for s in candidates:
        sid = s.get("subtask_id")
        if sid in used:
            continue
        blob = " ".join([
            str(s.get("subtask_id") or ""),
            str(s.get("description") or ""),
            str(s.get("context") or ""),
            str(s.get("workflow_id") or ""),
        ]).lower()
        if wid and wid in blob:
            return s
        if name and len(name) > 2 and name in blob:
            return s
    return None


def _normalize_multi_workflow(
    normalized: List[Dict[str, Any]],
    units: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """N независимых workflow → N full_workflow (+ prep/spec)."""
    prep = [s for s in normalized if s["artifact_mode"] == MODE_PREP]
    specs = [s for s in normalized if s["artifact_mode"] == MODE_SPEC]
    fulls = [
        s for s in normalized
        if s["artifact_mode"] in (MODE_FULL, "") or not s["artifact_mode"]
    ]
    # undecided тоже кандидаты на match
    undecided = [s for s in normalized if not s["artifact_mode"]]
    pool = fulls if fulls else undecided

    used: set = set()
    result_fulls: List[Dict[str, Any]] = []
    prep_ids = [p["subtask_id"] for p in prep]

    for i, unit in enumerate(units):
        matched = _match_unit_to_subtask(unit, pool, used)
        sid = f"dev_{i + 1:03d}"
        if matched:
            used.add(matched["subtask_id"])
            sid = matched["subtask_id"]
            desc = matched.get("description") or ""
            ctx = matched.get("context") or ""
        else:
            desc = ""
            ctx = ""

        description = (
            f"Собрать ОДИН импортируемый n8n workflow «{unit['name']}» "
            f"(workflow_id={unit['workflow_id']}). "
            "НЕ реализуй другие workflow из архитектуры в этой задаче — "
            "для каждого сценария отдельная developer-задача.\n"
            + (desc.strip() if desc else "")
        ).strip()

        item = {
            "subtask_id": sid,
            "description": description,
            "depends_on": list(prep_ids),
            "context": (
                f"workflow_id={unit['workflow_id']}; name={unit['name']}\n{ctx}"
            )[:6000],
            "artifact_mode": MODE_FULL,
            "assigned_node_names": list((matched or {}).get("assigned_node_names") or []),
            "workflow_id": unit["workflow_id"],
            "workflow_name": unit["name"],
        }
        result_fulls.append(item)

    leftover = [
        s for s in pool
        if s["subtask_id"] not in used and s["artifact_mode"] == MODE_SPEC
    ]
    logger.info(
        "📐 Multi-workflow: %s units → %s full_workflow задач",
        len(units), len(result_fulls),
    )
    return _ensure_unique_ids(prep + specs + leftover + result_fulls)


def normalize_developer_subtasks(
    subtasks: List[Any],
    *,
    has_blueprint: bool,
    workflow_units: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Нормализует subtasks: 1 workflow = 1 full_workflow; N workflows = N задач."""
    raw = [_as_dict(s) for s in subtasks]
    if not raw:
        return raw

    units = list(workflow_units or [])
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
            "workflow_id": s.get("workflow_id") or "",
            "workflow_name": s.get("workflow_name") or "",
        }
        normalized.append(item)

    if len(units) > 1:
        return _normalize_multi_workflow(normalized, units)

    if not has_blueprint and len(units) <= 1:
        for s in normalized:
            if not s["artifact_mode"]:
                s["artifact_mode"] = MODE_FULL
            if not s.get("workflow_id"):
                s["workflow_id"] = "wf_1"
        return _ensure_unique_ids(normalized)

    prep = [s for s in normalized if s["artifact_mode"] == MODE_PREP]
    specs = [s for s in normalized if s["artifact_mode"] == MODE_SPEC]
    fulls = [s for s in normalized if s["artifact_mode"] == MODE_FULL]
    undecided = [s for s in normalized if not s["artifact_mode"]]

    workflow_candidates = fulls + undecided
    if len(workflow_candidates) > 1:
        logger.warning(
            "⚠️ Декомпозиция: %s кандидатов на n8n workflow при одном blueprint — "
            "склеиваем в одну full_workflow (prep/spec сохраняем)",
            len(workflow_candidates),
        )
        owner = _collapse_workflow_slice(workflow_candidates)
        if units:
            owner["workflow_id"] = units[0]["workflow_id"]
            owner["workflow_name"] = units[0]["name"]
        result = prep + specs + [owner]
        if prep:
            owner["depends_on"] = [p["subtask_id"] for p in prep]
        return _ensure_unique_ids(result)

    if len(workflow_candidates) == 1:
        workflow_candidates[0]["artifact_mode"] = MODE_FULL
        if units and not workflow_candidates[0].get("workflow_id"):
            workflow_candidates[0]["workflow_id"] = units[0]["workflow_id"]
            workflow_candidates[0]["workflow_name"] = units[0]["name"]
        return _ensure_unique_ids(prep + specs + workflow_candidates)

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
        owner = {
            "subtask_id": "dev_workflow",
            "description": (
                "Собрать ЕДИНЫЙ импортируемый n8n workflow по workflow_blueprint архитектора"
            ),
            "depends_on": [p["subtask_id"] for p in prep],
            "context": "Source of truth — workflow_blueprint; учти результаты prep-задач",
            "artifact_mode": MODE_FULL,
            "assigned_node_names": [],
            "workflow_id": (units[0]["workflow_id"] if units else "wf_1"),
            "workflow_name": (units[0]["name"] if units else "main"),
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
    workflow_unit: Optional[Dict[str, Any]] = None,
    sibling_workflow_names: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """input_data для create_task: полный blueprint только у full_workflow."""
    mode = subtask.get("artifact_mode") or MODE_FULL
    payload: Dict[str, Any] = {
        "context": subtask.get("context") or "",
        "architecture_summary": (architecture_summary or "")[:2000],
        "artifact_mode": mode,
        "assigned_node_names": subtask.get("assigned_node_names") or [],
        "one_workflow_per_task": True,
    }
    wid = subtask.get("workflow_id") or (workflow_unit or {}).get("workflow_id") or ""
    wname = subtask.get("workflow_name") or (workflow_unit or {}).get("name") or ""
    if wid:
        payload["workflow_id"] = wid
    if wname:
        payload["workflow_name"] = wname
    if sibling_workflow_names:
        payload["sibling_workflows_do_not_implement"] = sibling_workflow_names

    if mode == MODE_FULL:
        if workflow_unit and isinstance(workflow_unit.get("blueprint"), dict):
            unit_handoff = {
                "workflow_id": workflow_unit.get("workflow_id"),
                "workflow_name": workflow_unit.get("name"),
                "workflow_blueprint": workflow_unit["blueprint"],
                "note": (
                    "Один workflow на эту задачу. Не сериализуй sibling-сценарии."
                ),
            }
            payload["workflow_blueprint"] = json.dumps(
                unit_handoff, ensure_ascii=False, indent=2
            )
        else:
            payload["workflow_blueprint"] = blueprint_str
    elif mode == MODE_SPEC and blueprint_str:
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
