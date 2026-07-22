"""Итерации проекта: замечания человека → PM-replan на том же project_id.

Вариант B (см. ANALYSIS Direction X): не создаём новый проект, а поднимаем
номер итерации, архивируем final_report и добавляем новые pending-задачи.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("ProjectIteration")

VALID_AGENTS = frozenset({
    "client_hunter", "lead_hunter", "sales", "analyst", "architect",
    "developer", "crm_customizer", "qa", "tech_writer",
})


def parse_metrics(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return dict(data) if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def get_project_iteration(project: Dict[str, Any]) -> int:
    """Текущий номер итерации (1 = первый прогон)."""
    raw = project.get("iteration")
    if raw is not None and str(raw).strip().isdigit():
        return max(1, int(raw))
    metrics = parse_metrics(project.get("metrics"))
    try:
        return max(1, int(metrics.get("iteration") or 1))
    except (TypeError, ValueError):
        return 1


def build_previous_tasks_digest(tasks: List[Dict[str, Any]], *, limit: int = 24) -> List[Dict[str, Any]]:
    """Краткий дайджест прошлых задач для PM."""
    digest = []
    # Свежие completed/failed важнее pending
    ordered = sorted(
        tasks,
        key=lambda t: (
            0 if t.get("status") == "completed" else 1,
            str(t.get("task_id") or ""),
        ),
    )
    for t in ordered[:limit]:
        out = t.get("output_data") or ""
        if isinstance(out, dict):
            out_s = json.dumps(out, ensure_ascii=False)[:800]
        else:
            out_s = str(out)[:800]
        digest.append({
            "task_id": t.get("task_id"),
            "agent_name": t.get("agent_name"),
            "status": t.get("status"),
            "description": (t.get("task_description") or "")[:400],
            "output_preview": out_s,
            "qa_feedback": (t.get("qa_feedback") or "")[:300],
        })
    return digest


def enrich_task_input(
    task_data: Dict[str, Any],
    *,
    project: Dict[str, Any],
    human_remarks: str,
    iteration: int,
    previous_by_id: Dict[str, Dict[str, Any]],
) -> str:
    """Собирает input_data новой задачи с контекстом прошлой итерации."""
    base: Dict[str, Any] = {}
    raw_in = task_data.get("input_data")
    if isinstance(raw_in, dict):
        base = dict(raw_in)
    elif isinstance(raw_in, str) and raw_in.strip():
        try:
            parsed = json.loads(raw_in)
            if isinstance(parsed, dict):
                base = parsed
        except json.JSONDecodeError:
            base = {"notes": raw_in[:1000]}

    prev_ids = base.get("previous_task_ids") or base.get("reuse_from") or []
    if isinstance(prev_ids, str):
        prev_ids = [prev_ids]
    previous_outputs = {}
    for pid in prev_ids:
        prev = previous_by_id.get(str(pid))
        if not prev:
            continue
        out = prev.get("output_data") or ""
        if isinstance(out, dict):
            out_s = json.dumps(out, ensure_ascii=False)[:6000]
        else:
            out_s = str(out)[:6000]
        previous_outputs[str(pid)] = {
            "agent_name": prev.get("agent_name"),
            "status": prev.get("status"),
            "output_data": out_s,
        }

    # Если PM не указал previous_task_ids — подмешиваем релевантные по agent_name
    if not previous_outputs:
        agent = task_data.get("agent_name")
        for pid, prev in previous_by_id.items():
            if prev.get("agent_name") == agent and prev.get("status") == "completed":
                out = prev.get("output_data") or ""
                out_s = (
                    json.dumps(out, ensure_ascii=False)[:6000]
                    if isinstance(out, dict)
                    else str(out)[:6000]
                )
                previous_outputs[pid] = {
                    "agent_name": agent,
                    "status": "completed",
                    "output_data": out_s,
                }
                if len(previous_outputs) >= 2:
                    break

    base.update({
        "project_goal": project.get("goal", ""),
        "client_name": project.get("client_name", ""),
        "project_name": project.get("project_name", ""),
        "project_iteration": iteration,
        "human_remarks": human_remarks[:4000],
        "previous_iteration_outputs": previous_outputs,
    })
    return json.dumps(base, ensure_ascii=False)


def normalize_iteration_task_ids(
    tasks_list: List[Dict[str, Any]],
    iteration: int,
) -> List[Dict[str, Any]]:
    """Гарантирует префикс iterN_ у task_id; depends_on только внутри нового графа."""
    prefix = f"iter{iteration}_"
    id_map: Dict[str, str] = {}
    for t in tasks_list:
        old = str(t.get("task_id") or "").strip()
        if not old:
            continue
        new = old if old.startswith(prefix) else f"{prefix}{old}"
        new = re.sub(r"[^\w.\-]+", "_", new)[:80]
        id_map[old] = new
        t["task_id"] = new

    new_ids = set(id_map.values())
    for t in tasks_list:
        deps = t.get("depends_on") or []
        if not isinstance(deps, list):
            deps = []
        fixed = []
        for d in deps:
            mapped = id_map.get(str(d), str(d))
            if mapped in new_ids:
                fixed.append(mapped)
            else:
                # Зависимость от задачи прошлой итерации — в previous_task_ids
                prev = t.setdefault("input_data", {})
                if not isinstance(prev, dict):
                    prev = {}
                    t["input_data"] = prev
                prev.setdefault("previous_task_ids", [])
                if isinstance(prev["previous_task_ids"], list) and str(d) not in prev["previous_task_ids"]:
                    prev["previous_task_ids"].append(str(d))
        t["depends_on"] = fixed
    return tasks_list


def prepare_iteration_metrics(
    project: Dict[str, Any],
    *,
    next_iteration: int,
    human_remarks: str,
) -> Tuple[Dict[str, Any], int]:
    """Архивирует отчёт и возвращает обновлённые metrics + next_iteration."""
    metrics = parse_metrics(project.get("metrics"))
    current = get_project_iteration(project)
    history = metrics.get("iteration_history")
    if not isinstance(history, list):
        history = []
    history.append({
        "iteration": current,
        "final_report": (project.get("final_report") or "")[:50000],
        "completed_at": project.get("completed_at") or "",
        "archived_at": datetime.now().isoformat(),
        "human_remarks_for_next": human_remarks[:4000],
    })
    metrics["iteration"] = next_iteration
    metrics["iteration_history"] = history[-20:]  # cap
    metrics["last_human_remarks"] = human_remarks[:2000]
    return metrics, next_iteration
