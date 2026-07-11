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
from flask import Flask, jsonify, request, send_from_directory
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
    """Статус агентства + задачи + логи текущего проекта."""
    current_project = orchestrator.current_project

    if current_project:
        tokens_used = current_project.get("tokens_used", 0) or 0
        token_budget = current_project.get("token_budget", Config.TOKEN_BUDGET) or Config.TOKEN_BUDGET
        status = current_project.get("status", "in_progress")
        client = current_project.get("client_name", "—")
        final_report = current_project.get("final_report", "") or ""
        metrics_str = current_project.get("metrics", "{}") or "{}"
        completed_at = current_project.get("completed_at", "") or ""
        phase = current_project.get("current_phase", "") or ""
        project_id = current_project.get("Id")
        project_name = current_project.get("project_name", "") or ""
        try:
            metrics = (
                json.loads(metrics_str)
                if isinstance(metrics_str, str) and metrics_str.strip()
                else (metrics_str if isinstance(metrics_str, dict) else {})
            )
        except json.JSONDecodeError:
            metrics = {}
    else:
        tokens_used = 0
        token_budget = Config.TOKEN_BUDGET
        status = "idle"
        client = Config.DEFAULT_CLIENT_NAME
        final_report = ""
        metrics = {}
        completed_at = ""
        phase = ""
        project_id = None
        project_name = ""

    tasks_payload = []
    review_tasks = []
    if project_id:
        try:
            for t in tasks_db.get_tasks_by_project(project_id):
                item = {
                    "id": t.get("Id"),
                    "task_id": t.get("task_id"),
                    "agent_name": t.get("agent_name"),
                    "task_description": (t.get("task_description") or "")[:300],
                    "status": t.get("status"),
                    "qa_approved": t.get("qa_approved"),
                    "qa_feedback": (t.get("qa_feedback") or "")[:800],
                    "iteration_count": t.get("iteration_count") or 0,
                    "max_iterations": t.get("max_iterations") or 3,
                    "tokens_used": t.get("tokens_used") or 0,
                    "depends_on": t.get("depends_on") or "[]",
                    "updated_at": t.get("updated_at") or "",
                    "has_n8n_json": False,
                    "workflow_name": None,
                    "output_preview": None,
                }
                parsed = _try_parse_json(t.get("output_data"))
                if parsed:
                    item["output_preview"] = (
                        parsed.get("summary")
                        or parsed.get("pm_comment")
                        or parsed.get("approach")
                        or ""
                    )[:240]
                    n8n = parsed.get("n8n_json")
                    if isinstance(n8n, dict) and n8n.get("nodes"):
                        item["has_n8n_json"] = True
                        item["workflow_name"] = (
                            parsed.get("workflow_name")
                            or n8n.get("name")
                            or t.get("task_id")
                        )
                tasks_payload.append(item)
                if t.get("status") in ("needs_human_review", "failed"):
                    review_tasks.append({
                        "task_id": t.get("task_id"),
                        "agent_name": t.get("agent_name"),
                        "status": t.get("status"),
                        "qa_feedback": (t.get("qa_feedback") or "")[:500],
                    })
        except Exception as e:
            logger.warning(f"⚠️ Не удалось загрузить задачи для status: {e}")

    logs_payload = []
    try:
        for fields in db.get_recent_records(limit=40):
            logs_payload.append({
                "agent_name": fields.get("agent_name"),
                "status": fields.get("status"),
                "task_description": (fields.get("task_description") or "")[:200],
                "tokens_used": fields.get("tokens_used") or 0,
                "timestamp": fields.get("timestamp") or fields.get("created_at") or "",
                "full_response": fields.get("full_response") or "",
            })
    except Exception as e:
        logger.debug(f"Логи недоступны: {e}")

    return jsonify({
        "running": orchestrator.agency_running,
        "tokens_used": tokens_used,
        "token_budget": token_budget,
        "status": status,
        "client": client,
        "project_id": project_id,
        "project_name": project_name,
        "phase": phase,
        "final_report": final_report,
        "metrics": metrics,
        "completed_at": completed_at,
        "tasks": tasks_payload,
        "review_tasks": review_tasks,
        "logs": logs_payload,
    })


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
            if not orchestrator.current_project:
                return jsonify({"error": "Нет активного проекта"}), 400
            project_id = orchestrator.current_project.get("Id")
            tasks = tasks_db.get_tasks_by_project(project_id)
            task = next((t for t in tasks if t.get("task_id") == task_id), None)
            if not task:
                return jsonify({"error": f"Задача {task_id} не найдена"}), 404
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
