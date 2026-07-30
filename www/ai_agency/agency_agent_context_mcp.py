# agency_agent_context_mcp.py
"""MCP-сервер памяти итераций агентов (Direction AG / AI).

Для ЛЮБОГО агента агентства: developer, architect, analyst, sales,
client_hunter, lead_hunter, qa, tech_writer, crm_customizer, PM.

Расположение: /home/rdpuser/www/ai_agency/agency_agent_context_mcp.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_AGENCY = Path(__file__).resolve().parent
if str(_AGENCY) not in sys.path:
    sys.path.insert(0, str(_AGENCY))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from core.agent_context import (  # noqa: E402
    build_retry_prompt_block,
    clear_task_context,
    get_agent_history,
    get_latest_task_for_agent,
    list_agents_with_context,
    list_project_contexts,
    load_task_context,
    record_attempt,
)

mcp = FastMCP("Agency Agent Context")

_KNOWN_AGENTS = (
    "pm", "developer", "architect", "analyst", "sales",
    "client_hunter", "lead_hunter", "qa", "tech_writer", "crm_customizer",
)


@mcp.tool()
def list_supported_agents() -> str:
    """Список ролей агентов, для которых работает память итераций / MCP."""
    return json.dumps(
        {
            "agents": list(_KNOWN_AGENTS),
            "note": (
                "TaskExecutor пишет/читает Agent Context для любой роли на retry. "
                "Используйте get_agent_history / list_agents_with_context по project_id."
            ),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def get_task_context(project_id: str, task_id: str) -> str:
    """Полный контекст задачи (любой агент): история попыток, open_issues."""
    ctx = load_task_context(project_id, task_id)
    return json.dumps(ctx, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
def get_retry_prompt(
    project_id: str,
    task_id: str,
    qa_feedback: str = "",
    agent_name: str = "",
) -> str:
    """Готовый блок текста, который агентство вшивает в prompt на retry."""
    ctx = load_task_context(project_id, task_id)
    block = build_retry_prompt_block(
        ctx, qa_feedback=qa_feedback, agent_name=agent_name
    )
    return block or "История попыток пуста — это первая итерация."


@mcp.tool()
def list_task_contexts(project_id: str) -> str:
    """Список задач проекта с памятью итераций (с полем agents)."""
    items = list_project_contexts(project_id)
    if not items:
        return f"Нет сохранённого контекста для project_id={project_id}."
    return json.dumps(items, ensure_ascii=False, indent=2)


@mcp.tool()
def list_agents_with_memory(project_id: str) -> str:
    """Какие агенты уже оставляли попытки в проекте (агрегат по agent_name)."""
    items = list_agents_with_context(project_id)
    if not items:
        return f"Нет памяти агентов для project_id={project_id}."
    return json.dumps(items, ensure_ascii=False, indent=2)


@mcp.tool()
def get_agent_history(project_id: str, agent_name: str, limit: int = 30) -> str:
    """История попыток конкретного агента по всем задачам проекта.

    agent_name: developer | architect | analyst | sales | client_hunter |
    lead_hunter | qa | tech_writer | crm_customizer | pm
    """
    rows = get_agent_history(project_id, agent_name, limit=max(1, min(int(limit), 100)))
    if not rows:
        return (
            f"Нет попыток агента «{agent_name}» в project_id={project_id}. "
            f"Известные роли: {', '.join(_KNOWN_AGENTS)}"
        )
    return json.dumps(rows, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
def get_latest_context_for_agent(project_id: str, agent_name: str) -> str:
    """Последний task-контекст, где указанный агент оставлял попытки."""
    ctx = get_latest_task_for_agent(project_id, agent_name)
    if not ctx:
        return f"Нет контекста для агента «{agent_name}» в project_id={project_id}."
    return json.dumps(ctx, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
def record_agent_attempt(
    project_id: str,
    task_id: str,
    agent_name: str,
    iteration: int,
    status: str,
    feedback: str = "",
    issues_json: str = "[]",
    artifact_json: str = "",
    notes: str = "",
) -> str:
    """Записать попытку ЛЮБОГО агента (отладка / внешние пайплайны).

    status: rejected | approved | error | completed | failed
    issues_json: JSON-массив строк
    artifact_json: JSON ответа агента (опционально)
    """
    try:
        issues = json.loads(issues_json) if issues_json.strip() else []
        if not isinstance(issues, list):
            issues = [str(issues)]
    except json.JSONDecodeError:
        issues = [issues_json]
    artifact = None
    if artifact_json.strip():
        try:
            artifact = json.loads(artifact_json)
        except json.JSONDecodeError:
            artifact = artifact_json
    ctx = record_attempt(
        project_id=project_id,
        task_id=task_id,
        agent_name=agent_name,
        iteration=iteration,
        status=status,
        feedback=feedback,
        issues=issues,
        artifact=artifact,
        notes=notes,
    )
    return json.dumps(
        {
            "ok": True,
            "agent_name": agent_name,
            "attempts": len(ctx.get("attempts") or []),
            "open_issues": ctx.get("open_issues") or [],
            "last_status": ctx.get("last_status"),
        },
        ensure_ascii=False,
        indent=2,
    )


@mcp.tool()
def clear_context(project_id: str, task_id: str) -> str:
    """Удалить память итераций по задаче."""
    ok = clear_task_context(project_id, task_id)
    return "cleared" if ok else "not_found"


if __name__ == "__main__":
    mcp.run()
