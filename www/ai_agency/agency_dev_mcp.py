# agency_dev_mcp.py — MCP «Agency Dev Assistant» (живёт в каталоге агентства)
import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Agency Dev Assistant")

_BASE = Path(__file__).resolve().parent
# ANALYSIS.md обычно на уровень выше (/home/rdpuser/www/ANALYSIS.md)
# или рядом с агентством; override: AGENCY_ANALYSIS_MD
_ARCH_CANDIDATES = [
    os.environ.get("AGENCY_ANALYSIS_MD", ""),
    str(_BASE.parent / "ANALYSIS.md"),
    str(_BASE / "ANALYSIS.md"),
]
ARCH_FILE = next((p for p in _ARCH_CANDIDATES if p and os.path.isfile(p)), _ARCH_CANDIDATES[1])
TASKS_FILE = str(_BASE / "dev_tasks.json")

if not os.path.exists(TASKS_FILE):
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump([], f)


@mcp.tool()
def get_project_architecture() -> str:
    """
    Читает документ с архитектурой ИИ-агентства.
    Используйте этот инструмент, чтобы понять роли агентов и структуру проекта.
    """
    if os.path.exists(ARCH_FILE):
        with open(ARCH_FILE, "r", encoding="utf-8") as f:
            return f.read()
    return (
        f"Файл ANALYSIS.md не найден (искали: {ARCH_FILE}). "
        "Задайте AGENCY_ANALYSIS_MD или положите ANALYSIS.md рядом с www/."
    )


@mcp.tool()
def add_task(task_description: str, priority: str = "medium", assignee: str = "unassigned") -> str:
    """
    Добавляет новую задачу в трекер разработки агентства.
    :param task_description: Описание задачи.
    :param priority: Приоритет (low, medium, high).
    :param assignee: Кому назначена (например, 'coder_agent', 'manager_agent').
    """
    with open(TASKS_FILE, "r+", encoding="utf-8") as f:
        tasks = json.load(f)
        new_task = {
            "id": len(tasks) + 1,
            "desc": task_description,
            "priority": priority,
            "assignee": assignee,
            "status": "todo",
        }
        tasks.append(new_task)
        f.seek(0)
        json.dump(tasks, f, indent=2, ensure_ascii=False)
    return f"Задача #{new_task['id']} успешно добавлена: {task_description}"


@mcp.tool()
def list_tasks(status_filter: str = "all") -> str:
    """
    Показывает список задач разработки.
    :param status_filter: Фильтр по статусу (all, todo, in_progress, done).
    """
    with open(TASKS_FILE, "r", encoding="utf-8") as f:
        tasks = json.load(f)

    if status_filter != "all":
        tasks = [t for t in tasks if t["status"] == status_filter]

    if not tasks:
        return "Задач не найдено."
    return json.dumps(tasks, indent=2, ensure_ascii=False)


@mcp.tool()
def update_task_status(task_id: int, new_status: str) -> str:
    """
    Обновляет статус задачи.
    :param task_id: ID задачи.
    :param new_status: Новый статус (todo, in_progress, done).
    """
    with open(TASKS_FILE, "r+", encoding="utf-8") as f:
        tasks = json.load(f)
        for task in tasks:
            if task["id"] == task_id:
                task["status"] = new_status
                f.seek(0)
                json.dump(tasks, f, indent=2, ensure_ascii=False)
                f.truncate()
                return f"Статус задачи #{task_id} обновлен на '{new_status}'."
    return f"Задача с ID {task_id} не найдена."


if __name__ == "__main__":
    mcp.run()
