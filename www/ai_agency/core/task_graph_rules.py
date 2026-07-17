"""Правила нормализации Task Graph после ответа PM.

Гарантирует бизнес-инварианты, которые LLM-PM может нарушить
(например, поиск клиентов без агента sales для текстов).
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("TaskGraphRules")

_LEAD_GEN_AGENTS = frozenset({"client_hunter", "lead_hunter"})

_SALES_AFTER_HUNTER_DESCRIPTION = (
    "Напиши персонализированные холодные сообщения и вопросы квалификации "
    "по найденным клиентам/лидам. Источники: client_hunter_context и/или "
    "leads_context, USP, client_hunter_handoff_to_sales / leads_handoff_to_sales. "
    "Если лидов нет — messages=[] и объясни в next_steps (не выдумывай компании)."
)


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return list(parsed) if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


def enforce_sales_after_hunters(
    tasks: List[Dict[str, Any]],
    excluded_agents: Optional[List[str]] = None,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Если в графе есть client_hunter/lead_hunter — sales обязателен и зависит от них.

    - Добавляет задачу sales, если её нет.
    - Дописывает depends_on на hunter task_id.
    - Убирает sales из excluded_agents.
    """
    excluded = [a for a in (excluded_agents or []) if a]
    normalized = [dict(t) for t in (tasks or []) if isinstance(t, dict)]

    hunter_ids = [
        t["task_id"]
        for t in normalized
        if t.get("agent_name") in _LEAD_GEN_AGENTS and t.get("task_id")
    ]
    if not hunter_ids:
        return normalized, excluded

    excluded = [a for a in excluded if a != "sales"]

    sales_tasks = [t for t in normalized if t.get("agent_name") == "sales"]
    if not sales_tasks:
        existing_ids = {t.get("task_id") for t in normalized}
        sales_id = "task_sales"
        n = 1
        while sales_id in existing_ids:
            sales_id = f"task_sales_{n}"
            n += 1
        normalized.append({
            "task_id": sales_id,
            "agent_name": "sales",
            "task_description": _SALES_AFTER_HUNTER_DESCRIPTION,
            "depends_on": list(hunter_ids),
            "input_data": {},
            "max_iterations": 3,
        })
        logger.info(
            "Task Graph: добавлен обязательный sales (%s) после %s",
            sales_id, hunter_ids,
        )
        return normalized, excluded

    for st in sales_tasks:
        deps = _as_list(st.get("depends_on"))
        for hid in hunter_ids:
            if hid not in deps:
                deps.append(hid)
        st["depends_on"] = deps
        desc = (st.get("task_description") or "").strip()
        if not desc:
            st["task_description"] = _SALES_AFTER_HUNTER_DESCRIPTION
    logger.info("Task Graph: sales зависит от hunter tasks %s", hunter_ids)
    return normalized, excluded
