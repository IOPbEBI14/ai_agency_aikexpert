"""Идентификаторы задач developer: placeholder vs сабтаски (в т.ч. iterN_)."""
from __future__ import annotations

import re
from typing import Any, Dict

# dev_001 | dev_workflow | iter2_dev_001 | iter3_dev_workflow
_DEV_SUBTASK_ID = re.compile(r"^(?:iter\d+_)?dev_", re.IGNORECASE)


def is_developer_subtask_id(task_id: str) -> bool:
    """True для подзадач декомпозиции developer (не placeholder из task graph)."""
    return bool(_DEV_SUBTASK_ID.match((task_id or "").strip()))


def is_developer_placeholder_task(task: Dict[str, Any]) -> bool:
    """Placeholder: agent=developer, id НЕ вида (iterN_)dev_* (напр. task_003, iter2_task_004)."""
    if (task.get("agent_name") or "").strip().lower() != "developer":
        return False
    return not is_developer_subtask_id(str(task.get("task_id") or ""))
