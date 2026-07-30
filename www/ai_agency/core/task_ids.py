"""Идентификаторы задач: placeholder vs сабтаски (developer / tech_writer, в т.ч. iterN_)."""
from __future__ import annotations

import re
from typing import Any, Dict

# dev_001 | dev_workflow | iter2_dev_001 | iter3_dev_workflow
_DEV_SUBTASK_ID = re.compile(r"^(?:iter\d+_)?dev_", re.IGNORECASE)
# tw_001 | iter3_tw_002
_TW_SUBTASK_ID = re.compile(r"^(?:iter\d+_)?tw_", re.IGNORECASE)
_ITER_PREFIX = re.compile(r"^(iter\d+_)", re.IGNORECASE)


def iteration_prefix_from_task_id(task_id: str) -> str:
    """``iter3_task_005`` → ``iter3_``; иначе пустая строка."""
    m = _ITER_PREFIX.match((task_id or "").strip())
    return m.group(1) if m else ""


def is_developer_subtask_id(task_id: str) -> bool:
    """True для подзадач декомпозиции developer (не placeholder из task graph)."""
    return bool(_DEV_SUBTASK_ID.match((task_id or "").strip()))


def is_developer_placeholder_task(task: Dict[str, Any]) -> bool:
    """Placeholder: agent=developer, id НЕ вида (iterN_)dev_* (напр. task_003)."""
    if (task.get("agent_name") or "").strip().lower() != "developer":
        return False
    return not is_developer_subtask_id(str(task.get("task_id") or ""))


def is_tech_writer_subtask_id(task_id: str) -> bool:
    """True для сабтасков документации ``tw_*`` / ``iterN_tw_*``."""
    return bool(_TW_SUBTASK_ID.match((task_id or "").strip()))


def is_tech_writer_placeholder_task(task: Dict[str, Any]) -> bool:
    """Placeholder tech_writer из task graph (не сабтаск tw_*)."""
    if (task.get("agent_name") or "").strip().lower() != "tech_writer":
        return False
    return not is_tech_writer_subtask_id(str(task.get("task_id") or ""))
