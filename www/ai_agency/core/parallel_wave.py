"""Выбор волны независимых задач для параллельного выполнения (Фаза 3.1).

Правила безопасности:
- лимит max_parallel;
- hunters (client/lead) — не больше одного в волне;
- architect — только в одиночной волне (создаёт subtasks);
- singleton-агенты (sales, analyst, qa, …) — не больше одного экземпляра;
- developer может идти несколькими prep/spec параллельно.
"""
from __future__ import annotations

from typing import Any, Dict, List

HUNTER_AGENTS = frozenset({"client_hunter", "lead_hunter"})
SOLO_AGENTS = frozenset({"architect"})
# Не больше одной задачи этого типа в волне (общий контекст / overwrite).
SINGLETON_AGENTS = frozenset({
    "sales",
    "analyst",
    "qa",
    "tech_writer",
    "crm_customizer",
    "client_hunter",
    "lead_hunter",
    "pm",
})


def select_parallel_wave(
    ready_tasks: List[Dict[str, Any]],
    max_parallel: int,
) -> List[Dict[str, Any]]:
    """Возвращает подмножество ready_tasks для одновременного запуска."""
    if not ready_tasks:
        return []
    limit = max(1, int(max_parallel or 1))
    if limit == 1 or len(ready_tasks) == 1:
        return ready_tasks[:1]

    wave: List[Dict[str, Any]] = []
    used_agents: set[str] = set()
    hunter_in_wave = False

    for task in ready_tasks:
        if len(wave) >= limit:
            break
        agent = (task.get("agent_name") or "").strip().lower()

        if agent in SOLO_AGENTS:
            # Architect не смешиваем с другими: либо он один, либо ждёт следующей итерации.
            if not wave:
                return [task]
            continue

        if agent in HUNTER_AGENTS:
            if hunter_in_wave:
                continue
            hunter_in_wave = True

        if agent in SINGLETON_AGENTS and agent in used_agents:
            continue

        wave.append(task)
        if agent:
            used_agents.add(agent)

    return wave or ready_tasks[:1]
