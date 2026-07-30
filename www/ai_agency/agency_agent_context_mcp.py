# agency_agent_context_mcp.py
"""MCP-сервер памяти итераций агентов (Direction AG).

Расположение: /home/rdpuser/www/ai_agency/agency_agent_context_mcp.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Каталог агентства (= директория этого файла) на PYTHONPATH
_AGENCY = Path(__file__).resolve().parent
if str(_AGENCY) not in sys.path:
    sys.path.insert(0, str(_AGENCY))

from mcp.server.fastmcp import FastMCP  # noqa: E402

from core.agent_context import (  # noqa: E402
    build_retry_prompt_block,
    clear_task_context,
    list_project_contexts,
    load_task_context,
    record_attempt,
)

mcp = FastMCP("Agency Agent Context")


@mcp.tool()
def get_task_context(project_id: str, task_id: str) -> str:
    """Полный контекст задачи: история попыток, open_issues, last status."""
    ctx = load_task_context(project_id, task_id)
    return json.dumps(ctx, ensure_ascii=False, indent=2, default=str)


@mcp.tool()
def get_retry_prompt(project_id: str, task_id: str, qa_feedback: str = "") -> str:
    """Готовый блок текста, который агентство вшивает в prompt на retry."""
    ctx = load_task_context(project_id, task_id)
    block = build_retry_prompt_block(ctx, qa_feedback=qa_feedback)
    return block or "История попыток пуста — это первая итерация."


@mcp.tool()
def list_task_contexts(project_id: str) -> str:
    """Список задач проекта, по которым есть память итераций."""
    items = list_project_contexts(project_id)
    if not items:
        return f"Нет сохранённого контекста для project_id={project_id}."
    return json.dumps(items, ensure_ascii=False, indent=2)


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
    """Записать попытку агента вручную (отладка / внешние пайплайны).

    status: rejected | approved | error | completed | failed
    issues_json: JSON-массив строк
    artifact_json: JSON ответа агента или n8n workflow (опционально)
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
