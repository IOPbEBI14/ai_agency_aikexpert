"""
Flask API для ИИ-агентства.
Обрабатывает HTTP-запросы и запускает Orchestrator в отдельном потоке.
"""

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from flask import Flask, Response, jsonify, request, send_from_directory
from flask_cors import CORS

from core.config import Config
from core.nocodb import NocoDBClient, ProjectsClient, TasksClient
from core.orchestrator import Orchestrator
from core.utils import call_llm, load_prompt, log_to_agent_logs, try_fix_truncated_json

_BASE_DIR = Path(__file__).resolve().parent
_WORKFLOWS_DIR = _BASE_DIR / "workflows"

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("Main")

# ==================== ИНИЦИАЛИЗАЦИЯ ====================
db = NocoDBClient()
projects_db = ProjectsClient()
tasks_db = TasksClient()
orchestrator = Orchestrator()

app = Flask(__name__)
CORS(app)

agency_thread = None

_PROXY_ALLOWED_PATH_RE = re.compile(r"^(\d+)?$")
_PROXY_ALLOWED_PARAMS = frozenset({"limit", "offset", "where", "sort"})
_PROXY_ALLOWED_METHODS = frozenset({"GET", "POST", "PATCH"})


def _try_parse_json(raw: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        fixed = try_fix_truncated_json(text)
        if not fixed:
            return None
        try:
            data = json.loads(fixed)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None


def _parse_metrics(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _build_tasks_payload(project_id: Any):
    """Собирает tasks_payload и review_tasks для проекта."""
    tasks_payload = []
    review_tasks = []
    if not project_id:
        return tasks_payload, review_tasks
    try:
        for t in tasks_db.get_tasks_by_project(project_id):
            agent_name = t.get("agent_name")
            item = {
                "id": t.get("Id"),
                "task_id": t.get("task_id"),
                "agent_name": agent_name,
                "task_description": (t.get("task_description") or "")[:600],
                "status": t.get("status"),
                "qa_approved": t.get("qa_approved"),
                "qa_feedback": (t.get("qa_feedback") or "")[:6000],
                "iteration_count": t.get("iteration_count") or 0,
                "max_iterations": t.get("max_iterations") or 3,
                "tokens_used": t.get("tokens_used") or 0,
                "depends_on": t.get("depends_on") or "[]",
                "updated_at": t.get("updated_at") or "",
                "has_n8n_json": False,
                "workflow_name": None,
                "output_preview": None,
                "artifact": None,
            }
            parsed = _try_parse_json(t.get("output_data"))
            if parsed:
                item["output_preview"] = (
                    parsed.get("summary")
                    or parsed.get("pm_comment")
                    or parsed.get("approach")
                    or ""
                )[:1500]
                n8n = parsed.get("n8n_json")
                if isinstance(n8n, dict) and n8n.get("nodes"):
                    item["has_n8n_json"] = True
                    item["workflow_name"] = (
                        parsed.get("workflow_name")
                        or n8n.get("name")
                        or t.get("task_id")
                    )
                item["artifact"] = _build_agent_artifact(agent_name, parsed)
            tasks_payload.append(item)
            if t.get("status") in ("needs_human_review", "failed"):
                review_tasks.append({
                    "task_id": t.get("task_id"),
                    "agent_name": agent_name,
                    "status": t.get("status"),
                    "qa_feedback": (t.get("qa_feedback") or "")[:500],
                })
    except Exception as e:
        logger.warning(f"⚠️ Не удалось загрузить задачи проекта {project_id}: {e}")
    return tasks_payload, review_tasks


def _build_agent_artifact(agent_name: Optional[str], parsed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Структурированный артефакт для карточек architect / tech_writer / crm_customizer."""
    if not agent_name or not parsed:
        return None

    if agent_name == "architect":
        systems = []
        for s in parsed.get("systems") or []:
            if isinstance(s, dict):
                systems.append({
                    "name": s.get("name") or "",
                    "role": s.get("role") or "",
                    "api_available": s.get("api_available"),
                    "limitations": s.get("limitations"),
                })
        data_flow = []
        for step in parsed.get("data_flow") or []:
            if isinstance(step, dict):
                data_flow.append({
                    "step": step.get("step"),
                    "from": step.get("from") or step.get("from_"),
                    "to": step.get("to"),
                    "trigger": step.get("trigger"),
                    "data": step.get("data"),
                    "transformation": step.get("transformation"),
                })
        handoff = parsed.get("handoff_to_developer") or {}
        blueprint = handoff.get("workflow_blueprint") if isinstance(handoff, dict) else None
        nodes = (blueprint or {}).get("nodes") if isinstance(blueprint, dict) else []
        return {
            "kind": "architecture",
            "title": parsed.get("summary") or "Архитектура",
            "summary": parsed.get("summary") or "",
            "approach": parsed.get("approach") or "",
            "systems": systems,
            "data_flow": data_flow,
            "tech_stack": parsed.get("tech_stack") or [],
            "estimated_complexity": parsed.get("estimated_complexity"),
            "estimated_time_hours": parsed.get("estimated_time_hours"),
            "risks": parsed.get("risks") or [],
            "recommendations": parsed.get("recommendations") or "",
            "blueprint_nodes_count": len(nodes) if isinstance(nodes, list) else 0,
            "downloadable": True,
            "download_name": "architecture",
        }

    if agent_name == "tech_writer":
        documents = []
        for doc in parsed.get("documents") or []:
            if not isinstance(doc, dict):
                continue
            sections = doc.get("sections") or []
            documents.append({
                "title": doc.get("title") or "Документ",
                "type": doc.get("type") or "",
                "audience": doc.get("audience") or "",
                "sections_count": len(sections) if isinstance(sections, list) else 0,
                "preview": (
                    (sections[0].get("content") or "")[:280]
                    if isinstance(sections, list) and sections and isinstance(sections[0], dict)
                    else ""
                ),
            })
        return {
            "kind": "documents",
            "title": parsed.get("summary") or "Документация",
            "summary": parsed.get("summary") or "",
            "documents": documents,
            "checklist": parsed.get("checklist") or [],
            "faq": [
                {"question": f.get("question"), "answer": f.get("answer")}
                for f in (parsed.get("faq") or [])
                if isinstance(f, dict)
            ][:20],
            "downloadable": True,
            "download_name": "tech_writer_docs",
        }

    if agent_name == "crm_customizer":
        entities = []
        for e in parsed.get("entities") or []:
            if isinstance(e, dict):
                entities.append(e.get("name") or e.get("entity_name") or str(e))
        pipelines = []
        for p in parsed.get("pipelines") or []:
            if isinstance(p, dict):
                pipelines.append(p.get("name") or p.get("pipeline_name") or str(p))
        return {
            "kind": "crm_setup",
            "title": parsed.get("summary") or "Настройка CRM",
            "summary": parsed.get("summary") or "",
            "platform": parsed.get("platform") or "",
            "setup_steps": parsed.get("setup_steps") or [],
            "entities": entities,
            "pipelines": pipelines,
            "field_mapping_count": len(parsed.get("field_mapping") or []),
            "notes": parsed.get("notes") or "",
            "downloadable": True,
            "download_name": "crm_setup_guide",
        }

    return None


def _artifact_to_markdown(agent_name: str, parsed: Dict[str, Any]) -> str:
    """Собирает markdown-файл из output_data агента."""
    lines: list = []

    if agent_name == "architect":
        lines.append(f"# {parsed.get('summary') or 'Архитектура'}")
        lines.append("")
        if parsed.get("approach"):
            lines.append("## Подход")
            lines.append(str(parsed["approach"]))
            lines.append("")
        if parsed.get("tech_stack"):
            lines.append("## Стек")
            for item in parsed["tech_stack"]:
                lines.append(f"- {item}")
            lines.append("")
        if parsed.get("systems"):
            lines.append("## Системы")
            for s in parsed["systems"]:
                if not isinstance(s, dict):
                    continue
                lim = f" — {s.get('limitations')}" if s.get("limitations") else ""
                lines.append(
                    f"- **{s.get('name')}** ({s.get('role')})"
                    f"{', API' if s.get('api_available') else ''}{lim}"
                )
            lines.append("")
        if parsed.get("data_flow"):
            lines.append("## Поток данных")
            for step in parsed["data_flow"]:
                if not isinstance(step, dict):
                    continue
                fr = step.get("from") or step.get("from_")
                lines.append(
                    f"{step.get('step')}. {fr} → {step.get('to')} "
                    f"(триггер: {step.get('trigger')})"
                )
                if step.get("data"):
                    lines.append(f"   - Данные: {step['data']}")
                if step.get("transformation"):
                    lines.append(f"   - Трансформация: {step['transformation']}")
            lines.append("")
        if parsed.get("risks"):
            lines.append("## Риски")
            for r in parsed["risks"]:
                lines.append(f"- {r}")
            lines.append("")
        if parsed.get("recommendations"):
            lines.append("## Рекомендации")
            lines.append(str(parsed["recommendations"]))
            lines.append("")
        handoff = parsed.get("handoff_to_developer")
        if isinstance(handoff, dict) and handoff.get("workflow_blueprint"):
            bp = handoff["workflow_blueprint"]
            lines.append("## Workflow blueprint")
            for node in bp.get("nodes") or []:
                if isinstance(node, dict):
                    lines.append(
                        f"- `{node.get('name')}` ({node.get('type')}): {node.get('purpose') or ''}"
                    )
            lines.append("")

    elif agent_name == "tech_writer":
        lines.append(f"# {parsed.get('summary') or 'Документация'}")
        lines.append("")
        for doc in parsed.get("documents") or []:
            if not isinstance(doc, dict):
                continue
            lines.append(f"## {doc.get('title') or 'Документ'}")
            lines.append(f"*Тип: {doc.get('type') or '—'} · Аудитория: {doc.get('audience') or '—'}*")
            lines.append("")
            for sec in doc.get("sections") or []:
                if not isinstance(sec, dict):
                    continue
                lines.append(f"### {sec.get('title') or 'Раздел'}")
                lines.append(str(sec.get("content") or ""))
                lines.append("")
        if parsed.get("faq"):
            lines.append("## FAQ")
            for item in parsed["faq"]:
                if isinstance(item, dict):
                    lines.append(f"**Q:** {item.get('question')}")
                    lines.append(f"**A:** {item.get('answer')}")
                    lines.append("")
        if parsed.get("checklist"):
            lines.append("## Чек-лист")
            for step in parsed["checklist"]:
                lines.append(f"- [ ] {step}")
            lines.append("")

    elif agent_name == "crm_customizer":
        lines.append(f"# {parsed.get('summary') or 'Настройка CRM'}")
        lines.append("")
        lines.append(f"**Платформа:** {parsed.get('platform') or '—'}")
        lines.append("")
        if parsed.get("setup_steps"):
            lines.append("## Инструкция по настройке")
            for i, step in enumerate(parsed["setup_steps"], 1):
                lines.append(f"{i}. {step}")
            lines.append("")
        if parsed.get("entities"):
            lines.append("## Сущности")
            for e in parsed["entities"]:
                if isinstance(e, dict):
                    lines.append(f"- {e.get('name') or e}")
                else:
                    lines.append(f"- {e}")
            lines.append("")
        if parsed.get("pipelines"):
            lines.append("## Воронки")
            for p in parsed["pipelines"]:
                if isinstance(p, dict):
                    lines.append(f"- {p.get('name') or p}")
                else:
                    lines.append(f"- {p}")
            lines.append("")
        if parsed.get("field_mapping"):
            lines.append("## Маппинг полей")
            for m in parsed["field_mapping"]:
                if isinstance(m, dict):
                    lines.append(
                        f"- {m.get('source') or m.get('from') or '?'} → "
                        f"{m.get('target') or m.get('to') or '?'}"
                    )
            lines.append("")
        if parsed.get("notes"):
            lines.append("## Заметки")
            lines.append(str(parsed["notes"]))
            lines.append("")

    else:
        lines.append("# Результат агента")
        lines.append("```json")
        lines.append(json.dumps(parsed, ensure_ascii=False, indent=2))
        lines.append("```")

    return "\n".join(lines).strip() + "\n"


def _build_pm_payload(project: Dict[str, Any], tasks_payload: list, review_tasks: list) -> Dict[str, Any]:
    reasoning = project.get("reasoning") or ""
    if isinstance(reasoning, str) and len(reasoning) > 1200:
        reasoning = reasoning[:1200]
    return {
        "project_name": project.get("project_name") or "",
        "phase": project.get("current_phase") or "",
        "status": project.get("status") or "",
        "client": project.get("client_name") or "",
        "tokens_used": project.get("tokens_used") or 0,
        "token_budget": project.get("token_budget") or Config.TOKEN_BUDGET,
        "reasoning": reasoning,
        "goal": (project.get("goal") or "")[:800],
        "active_agents": [
            t.get("agent_name") for t in tasks_payload if t.get("status") == "in_progress"
        ],
        "tasks_total": len(tasks_payload),
        "tasks_completed": sum(1 for t in tasks_payload if t.get("status") == "completed"),
        "tasks_failed": sum(1 for t in tasks_payload if t.get("status") == "failed"),
        "review_count": len(review_tasks),
    }


def _project_view_payload(project: Dict[str, Any]) -> Dict[str, Any]:
    """Единый payload для live status и просмотра истории."""
    project_id = project.get("Id")
    tasks_payload, review_tasks = _build_tasks_payload(project_id)
    metrics = _parse_metrics(project.get("metrics"))
    return {
        "project_id": project_id,
        "project_name": project.get("project_name") or "",
        "client": project.get("client_name") or "—",
        "status": project.get("status") or "",
        "phase": project.get("current_phase") or "",
        "tokens_used": project.get("tokens_used") or 0,
        "token_budget": project.get("token_budget") or Config.TOKEN_BUDGET,
        "final_report": project.get("final_report") or "",
        "metrics": metrics,
        "completed_at": project.get("completed_at") or "",
        "goal": project.get("goal") or "",
        "tasks": tasks_payload,
        "review_tasks": review_tasks,
        "pm": _build_pm_payload(project, tasks_payload, review_tasks),
    }


def _safe_filename(name: str) -> str:
    name = (name or "workflow").strip().replace(" ", "_")
    name = re.sub(r"[^\w.\-а-яА-ЯёЁ]+", "", name, flags=re.UNICODE)
    return name[:120] or "workflow"


# ==================== FLASK API ====================

@app.route("/")
def serve_dashboard():
    """Отдаёт дашборд агентства."""
    return send_from_directory(_BASE_DIR, "index.html")


@app.route("/api/agency/status", methods=["GET"])
def get_status():
    """Статус агентства + задачи текущего проекта."""
    current_project = orchestrator.current_project

    if current_project:
        view = _project_view_payload(current_project)
        view["running"] = orchestrator.agency_running
        return jsonify(view)

    return jsonify({
        "running": orchestrator.agency_running,
        "tokens_used": 0,
        "token_budget": Config.TOKEN_BUDGET,
        "status": "idle",
        "client": Config.DEFAULT_CLIENT_NAME,
        "project_id": None,
        "project_name": "",
        "phase": "",
        "final_report": "",
        "metrics": {},
        "completed_at": "",
        "goal": "",
        "tasks": [],
        "review_tasks": [],
        "pm": {
            "project_name": "",
            "phase": "",
            "status": "idle",
            "client": Config.DEFAULT_CLIENT_NAME,
            "tokens_used": 0,
            "token_budget": Config.TOKEN_BUDGET,
            "reasoning": "",
            "goal": "",
            "active_agents": [],
            "tasks_total": 0,
            "tasks_completed": 0,
            "tasks_failed": 0,
            "review_count": 0,
        },
    })


@app.route("/api/agency/projects", methods=["GET"])
def list_projects():
    """Список проектов для просмотра истории запусков."""
    limit = request.args.get("limit", 50, type=int) or 50
    limit = max(1, min(limit, 100))
    projects = projects_db.list_projects(limit=limit)
    current_id = None
    if orchestrator.current_project:
        current_id = orchestrator.current_project.get("Id")

    items = []
    for p in projects:
        pid = p.get("Id")
        items.append({
            "id": pid,
            "project_name": p.get("project_name") or f"project-{pid}",
            "client_name": p.get("client_name") or "—",
            "status": p.get("status") or "",
            "phase": p.get("current_phase") or "",
            "tokens_used": p.get("tokens_used") or 0,
            "token_budget": p.get("token_budget") or 0,
            "completed_at": p.get("completed_at") or "",
            "updated_at": p.get("updated_at") or p.get("UpdatedAt") or p.get("updateTime") or "",
            "has_final_report": bool((p.get("final_report") or "").strip()),
            "is_current": pid is not None and pid == current_id,
            "goal": (p.get("goal") or "")[:200],
        })
    return jsonify({"projects": items, "current_project_id": current_id})


@app.route("/api/agency/projects/<int:project_id>", methods=["GET"])
def get_project(project_id: int):
    """Детали выбранного проекта: задачи, PM, итоговый отчёт."""
    project = projects_db.find_project_by_id(project_id)
    if not project:
        return jsonify({"error": f"Проект {project_id} не найден"}), 404
    view = _project_view_payload(project)
    current_id = None
    if orchestrator.current_project:
        current_id = orchestrator.current_project.get("Id")
    view["running"] = False
    view["is_current"] = project_id == current_id
    view["view_mode"] = "history"
    return jsonify(view)


@app.route("/api/agency/artifacts/download", methods=["GET"])
def download_artifact():
    """Скачать markdown-артефакт задачи (architect / tech_writer / crm_customizer).

    Query:
      project_id: int
      task_id: str
    """
    project_id = request.args.get("project_id", type=int)
    task_id = request.args.get("task_id", type=str)
    if not project_id or not task_id:
        return jsonify({"error": "project_id и task_id обязательны"}), 400

    tasks = tasks_db.get_tasks_by_project(project_id)
    task = next((t for t in tasks if t.get("task_id") == task_id), None)
    if not task:
        return jsonify({"error": f"Задача {task_id} не найдена"}), 404

    agent_name = task.get("agent_name") or ""
    if agent_name not in ("architect", "tech_writer", "crm_customizer"):
        return jsonify({"error": f"Скачивание не поддерживается для агента {agent_name}"}), 400

    parsed = _try_parse_json(task.get("output_data"))
    if not parsed:
        return jsonify({"error": "У задачи нет валидного output_data"}), 400

    markdown = _artifact_to_markdown(agent_name, parsed)
    names = {
        "architect": "architecture",
        "tech_writer": "tech_writer_docs",
        "crm_customizer": "crm_setup_guide",
    }
    filename = f"{names.get(agent_name, agent_name)}_{task_id}.md"
    return Response(
        markdown,
        mimetype="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@app.route("/api/agency/start", methods=["POST"])
def start_agency():
    """Запуск агентства."""
    global agency_thread

    if orchestrator.agency_running:
        return jsonify({"error": "Already running"}), 400

    if not orchestrator.initialize():
        return jsonify({"error": "Failed to initialize project"}), 500

    orchestrator.agency_running = True
    agency_thread = threading.Thread(target=orchestrator.run, daemon=True)
    agency_thread.start()

    return jsonify({
        "status": "started",
        "project": orchestrator.current_project.get("project_name"),
        "phase": orchestrator.current_project.get("current_phase"),
        "tokens_used": orchestrator.current_project.get("tokens_used", 0),
        "token_budget": orchestrator.current_project.get("token_budget", Config.TOKEN_BUDGET),
    })


@app.route("/api/agency/stop", methods=["POST"])
def stop_agency():
    """Остановка оркестратора."""
    orchestrator.stop()
    return jsonify({"status": "stopped"})


@app.route("/api/agency/resume", methods=["POST"])
def resume_agency():
    """Возобновить работу агентства."""
    global agency_thread

    if orchestrator.agency_running:
        return jsonify({"error": "Already running"}), 400

    if not orchestrator.initialize():
        return jsonify({"error": "Failed to initialize project"}), 500

    if orchestrator.current_project.get("status") == "needs_human_review":
        project_id = orchestrator.current_project.get("Id")
        if project_id:
            projects_db.update_project(project_id, {"status": "in_progress"})
        orchestrator.current_project["status"] = "in_progress"

    orchestrator.agency_running = True
    agency_thread = threading.Thread(target=orchestrator.run, daemon=True)
    agency_thread.start()

    return jsonify({
        "status": "resumed",
        "phase": orchestrator.current_project.get("current_phase"),
    })


@app.route("/api/agency/increase-tokens", methods=["POST"])
def increase_tokens():
    """Увеличить бюджет токенов."""
    if not orchestrator.current_project:
        orchestrator.initialize()

    initial_budget = Config.TOKEN_BUDGET
    current_budget = orchestrator.current_project.get("token_budget", 0) or 0
    new_budget = current_budget + initial_budget

    if orchestrator.current_project.get("Id"):
        projects_db.update_project(orchestrator.current_project["Id"], {"token_budget": new_budget})
    orchestrator.current_project["token_budget"] = new_budget

    logger.info(f"💰 Бюджет увеличен: {current_budget} → {new_budget}")
    return jsonify({"status": "success", "new_budget": new_budget, "added": initial_budget})


@app.route("/api/agency/human-review", methods=["POST"])
def human_review():
    """Ручное вмешательство: комментарий к задаче и/или проекту.

    Body:
      human_prompt: str (обязательно)
      task_id: str (опционально) — перезапуск конкретной задачи
      resume: bool (default true)
    """
    global agency_thread
    data = request.get_json() or {}
    task_id = data.get("task_id")
    human_prompt = (data.get("human_prompt") or "").strip()
    should_resume = data.get("resume", True)

    if not human_prompt:
        return jsonify({"error": "human_prompt обязателен"}), 400

    if not orchestrator.current_project:
        return jsonify({"error": "Нет активного проекта"}), 400

    project_id = orchestrator.current_project.get("Id")
    if not project_id:
        return jsonify({"error": "У проекта нет Id"}), 400

    try:
        result = {
            "status": "success",
            "project_id": project_id,
            "task_id": task_id,
            "pm_comment": "",
            "updated_description": None,
            "resumed": False,
        }

        if task_id:
            tasks = tasks_db.get_tasks_by_project(project_id)
            task = next((t for t in tasks if t.get("task_id") == task_id), None)
            if not task:
                return jsonify({"error": f"Задача {task_id} не найдена"}), 404

            pm_prompt = load_prompt("pm")
            pm_task = f"""
ЧЕЛОВЕК ВМЕШАЛСЯ В ЗАДАЧУ {task_id}.

ИСХОДНАЯ ЗАДАЧА:
{task.get('task_description')}

РЕЗУЛЬТАТ АГЕНТА ({task.get('agent_name')}):
{(task.get('output_data') or 'Нет данных')[:2000]}

РЕЗУЛЬТАТ QA / ПРИЧИНА ОСТАНОВКИ:
{(task.get('qa_feedback') or 'Нет данных')[:1000]}

УКАЗАНИЯ ЧЕЛОВЕКА:
{human_prompt}

ТВОЯ ЗАДАЧА:
1. Проанализируй указания человека
2. Скорректируй задачу для агента {task.get('agent_name')}
3. Верни обновлённое описание задачи

ФОРМАТ ОТВЕТА (строго JSON):
{{
    "updated_task_description": "новое описание задачи",
    "pm_comment": "комментарий"
}}

Верни ТОЛЬКО валидный JSON.
"""
            pm_response, pm_tokens = call_llm("PM", pm_prompt, pm_task)
            orchestrator.current_project["tokens_used"] = (
                orchestrator.current_project.get("tokens_used", 0) or 0
            ) + pm_tokens
            projects_db.update_project(
                project_id, {"tokens_used": orchestrator.current_project["tokens_used"]}
            )

            pm_decision = _try_parse_json(pm_response) or {}
            updated_description = pm_decision.get(
                "updated_task_description", task.get("task_description")
            )
            pm_comment = pm_decision.get("pm_comment", "")

            task_db_id = task.get("Id")
            if task_db_id:
                tasks_db.update_task(task_db_id, {
                    "status": "pending",
                    "iteration_count": 0,
                    "qa_approved": "pending",
                    "qa_feedback": f"Human review: {human_prompt[:400]}",
                    "task_description": updated_description,
                    "output_data": None,
                })

            log_to_agent_logs(
                project_id=project_id,
                agent_name="PM",
                status="completed",
                task_description=f"Human review для задачи {task_id}: {pm_comment}",
                full_response=json.dumps({
                    "human_prompt": human_prompt,
                    "pm_decision": pm_decision,
                }, ensure_ascii=False),
                tokens_used=pm_tokens,
            )
            result["pm_comment"] = pm_comment
            result["updated_description"] = updated_description
            logger.info(f"✅ Human review для задачи {task_id}: {pm_comment}")
        else:
            log_to_agent_logs(
                project_id=project_id,
                agent_name="PM",
                status="completed",
                task_description=f"Human review (проект): {human_prompt[:200]}",
                full_response=json.dumps({"human_prompt": human_prompt}, ensure_ascii=False),
                tokens_used=0,
            )
            result["pm_comment"] = "Комментарий принят, проект возвращается в работу"

        projects_db.update_project(project_id, {"status": "in_progress"})
        orchestrator.current_project["status"] = "in_progress"

        if should_resume and not orchestrator.agency_running:
            orchestrator.agency_running = True
            agency_thread = threading.Thread(target=orchestrator.run, daemon=True)
            agency_thread.start()
            result["resumed"] = True

        return jsonify(result)

    except Exception as e:
        logger.error(f"❌ Ошибка human review: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/agency/workflows", methods=["GET"])
def list_workflows():
    """Список сохранённых n8n workflow файлов."""
    _WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for path in sorted(_WORKFLOWS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        files.append({
            "name": path.name,
            "size": path.stat().st_size,
            "modified": datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
        })
    return jsonify({"workflows": files})


@app.route("/api/agency/workflows/save", methods=["POST"])
def save_workflow():
    """Сохраняет n8n workflow из задачи developer или из переданного JSON."""
    data = request.get_json() or {}
    task_id = data.get("task_id")
    n8n_json = data.get("n8n_json")
    filename = data.get("filename")

    try:
        if task_id:
            project_id = data.get("project_id")
            if not project_id:
                if not orchestrator.current_project:
                    return jsonify({"error": "Нет активного проекта"}), 400
                project_id = orchestrator.current_project.get("Id")
            tasks = tasks_db.get_tasks_by_project(project_id)
            task = next((t for t in tasks if t.get("task_id") == task_id), None)
            if not task:
                return jsonify({"error": f"Задача {task_id} не найдена в проекте {project_id}"}), 404
            parsed = _try_parse_json(task.get("output_data"))
            if not parsed:
                return jsonify({"error": "output_data задачи не является валидным JSON"}), 400
            n8n_json = parsed.get("n8n_json")
            if not isinstance(n8n_json, dict) or not n8n_json.get("nodes"):
                return jsonify({"error": "В output_data нет n8n_json с nodes"}), 400
            filename = filename or (
                parsed.get("workflow_name") or n8n_json.get("name") or task_id
            )

        if not isinstance(n8n_json, dict) or not n8n_json.get("nodes"):
            return jsonify({"error": "n8n_json обязателен и должен содержать nodes"}), 400

        safe_name = _safe_filename(filename or n8n_json.get("name") or "workflow")
        if not safe_name.endswith(".json"):
            safe_name += ".json"

        _WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = _WORKFLOWS_DIR / safe_name
        out_path.write_text(
            json.dumps(n8n_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"💾 Workflow сохранён: {out_path}")
        return jsonify({
            "status": "success",
            "filename": safe_name,
            "path": str(out_path.relative_to(_BASE_DIR)),
            "nodes_count": len(n8n_json.get("nodes") or []),
        })
    except Exception as e:
        logger.error(f"❌ Ошибка сохранения workflow: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/api/agency/workflows/<path:filename>", methods=["GET"])
def download_workflow(filename):
    """Скачать сохранённый workflow."""
    safe = _safe_filename(filename)
    if not safe.endswith(".json"):
        safe += ".json"
    path = _WORKFLOWS_DIR / safe
    if not path.exists():
        return jsonify({"error": "Файл не найден"}), 404
    return send_from_directory(_WORKFLOWS_DIR, safe, as_attachment=True)


@app.route("/api/nocodb", methods=["GET", "POST", "PATCH"], strict_slashes=False)
@app.route("/api/nocodb/<path:path>", methods=["GET", "POST", "PATCH"])
def nocodb_proxy(path=""):
    """Read/write прокси для NocoDB API v3 (только agent_logs)."""
    if not _PROXY_ALLOWED_PATH_RE.match(path):
        logger.warning(f"🚫 Прокси: отклонён путь '{path}'")
        return jsonify({"error": "Forbidden path"}), 403

    if request.method not in _PROXY_ALLOWED_METHODS:
        return jsonify({"error": "Method not allowed"}), 405

    nocodb_url = Config.get_nocodb_records_url()
    if path:
        nocodb_url = f"{nocodb_url}/{path}"

    filtered_params = {
        k: v for k, v in request.args.items() if k in _PROXY_ALLOWED_PARAMS
    }
    if filtered_params:
        query_string = "&".join(f"{k}={v}" for k, v in filtered_params.items())
        nocodb_url += f"?{query_string}"

    headers = {"xc-token": Config.NOCODB_API_TOKEN, "Content-Type": "application/json"}

    try:
        resp = requests.request(
            method=request.method,
            url=nocodb_url,
            headers=headers,
            json=request.get_json(silent=True),
            timeout=30,
        )
        return resp.content, resp.status_code, dict(resp.headers)
    except requests.exceptions.RequestException as e:
        logger.error(f"❌ Ошибка прокси в NocoDB: {e}")
        return jsonify({"error": str(e)}), 502


# ==================== ЗАПУСК ====================

def test_llm_connection():
    """Проверка подключения к Yandex AI Studio."""
    logger.info("🔍 Проверка подключения к Yandex AI Studio...")
    logger.info(f"   URL: {Config.get_llm_responses_url()}")
    logger.info(f"   Model: {Config.get_llm_model_uri()}")
    api_key_preview = (
        f"{'*' * 8}{Config.LLM_API_KEY[-4:]}"
        if Config.LLM_API_KEY and len(Config.LLM_API_KEY) > 4
        else "NOT SET"
    )
    logger.info(f"   API Key: {api_key_preview}")

    if not Config.LLM_API_KEY or not Config.LLM_FOLDER_ID:
        logger.error("❌ Не указан LLM_API_KEY или LLM_FOLDER_ID в .env")
        return False

    try:
        content, tokens = call_llm("TEST", "Ты тестовый агент.", "Скажи 'OK' одним словом.")
        logger.info(f"✅ LLM подключен! Ответ: {content[:50]}")
        return True
    except Exception as e:
        logger.error(f"❌ LLM недоступен: {e}")
        return False


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("🚀 Flask API запускается на порту 5000...")
    logger.info("=" * 60)

    test_llm_connection()
    _WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)

    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)
