"""
Flask API для ИИ-агентства.
Обрабатывает HTTP-запросы и запускает Orchestrator в отдельном потоке.
"""

import os
import json
import logging
import threading
from datetime import datetime
from typing import Dict, Any, Optional
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests

from core.config import Config
from core.nocodb import NocoDBClient, ProjectsClient, TasksClient
from core.orchestrator import Orchestrator
from core.utils import validate_with_qa, log_to_agent_logs, update_last_agent_log, load_prompt, call_llm, build_agent_task, try_fix_truncated_json

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
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


# ==================== FLASK API ====================

@app.route('/api/agency/status', methods=['GET'])
def get_status():
    """Статус работы агентства."""
    current_project = orchestrator.current_project
    
    if current_project:
        tokens_used = current_project.get("tokens_used", 0) or 0
        token_budget = current_project.get("token_budget", Config.TOKEN_BUDGET) or Config.TOKEN_BUDGET
        status = current_project.get("status", "in_progress")
        client = current_project.get("client_name", "—")
        final_report = current_project.get("final_report", "") or ""
        metrics_str = current_project.get("metrics", "{}") or "{}"
        completed_at = current_project.get("completed_at", "") or ""
        
        try:
            metrics = json.loads(metrics_str) if isinstance(metrics_str, str) and metrics_str.strip() else {}
        except json.JSONDecodeError:
            metrics = {}
    else:
        tokens_used = 0
        token_budget = Config.TOKEN_BUDGET
        status = "in_progress"
        client = Config.DEFAULT_CLIENT_NAME
        final_report = ""
        metrics = {}
        completed_at = ""
    
    return jsonify({
        "running": orchestrator.agency_running,
        "tokens_used": tokens_used,
        "token_budget": token_budget,
        "status": status,
        "client": client,
        "final_report": final_report,
        "metrics": metrics,
        "completed_at": completed_at
    })


@app.route('/api/agency/start', methods=['POST'])
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
        "token_budget": orchestrator.current_project.get("token_budget", Config.TOKEN_BUDGET)
    })


@app.route('/api/agency/stop', methods=['POST'])
def stop_agency():
    """Остановка оркестратора."""
    orchestrator.stop()
    return jsonify({"status": "stopped"})


@app.route('/api/agency/resume', methods=['POST'])
def resume_agency():
    """Возобновить работу агентства."""
    global agency_thread
    
    if orchestrator.agency_running:
        return jsonify({"error": "Already running"}), 400
    
    if not orchestrator.initialize():
        return jsonify({"error": "Failed to initialize project"}), 500
    
    orchestrator.agency_running = True
    agency_thread = threading.Thread(target=orchestrator.run, daemon=True)
    agency_thread.start()
    
    return jsonify({"status": "resumed", "phase": orchestrator.current_project.get("current_phase")})


@app.route('/api/agency/increase-tokens', methods=['POST'])
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


@app.route('/api/agency/human-review', methods=['POST'])
def human_review():
    """Обработка ручного вмешательства в задачу."""
    data = request.get_json()
    task_id = data.get('task_id')
    human_prompt = data.get('human_prompt')
    
    if not task_id or not human_prompt:
        return jsonify({"error": "task_id и human_prompt обязательны"}), 400
    
    if not orchestrator.current_project:
        return jsonify({"error": "Нет активного проекта"}), 400
    
    project_id = orchestrator.current_project.get("Id")
    if not project_id:
        return jsonify({"error": "У проекта нет Id"}), 400
    
    try:
        tasks = tasks_db.get_tasks_by_project(project_id)
        task = next((t for t in tasks if t.get("task_id") == task_id), None)
        
        if not task:
            return jsonify({"error": f"Задача {task_id} не найдена"}), 404
        
        pm_prompt = load_prompt("pm")
        
        pm_task = f"""
ЧЕЛОВЕК ВМЕШАЛСЯ В ЗАДАЧУ {task_id}.

ИСХОДНАЯ ЗАДАЧА:
{task.get('task_description')}

РЕЗУЛЬТАТ DEVELOPER:
{task.get('output_data', 'Нет данных')[:2000]}

РЕЗУЛЬТАТ QA:
{task.get('qa_feedback', 'Нет данных')[:1000]}

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
        
        orchestrator.current_project["tokens_used"] = (orchestrator.current_project.get("tokens_used", 0) or 0) + pm_tokens
        projects_db.update_project(project_id, {"tokens_used": orchestrator.current_project["tokens_used"]})
        
        pm_decision = json.loads(pm_response)
        updated_description = pm_decision.get("updated_task_description", task.get("task_description"))
        pm_comment = pm_decision.get("pm_comment", "")
        
        task_db_id = task.get("Id")
        if task_db_id:
            tasks_db.update_task(task_db_id, {
                "status": "pending",
                "iteration_count": 0,
                "qa_approved": "pending",
                "qa_feedback": "",
                "task_description": updated_description,
                "output_data": None
            })
        
        log_to_agent_logs(
            project_id=project_id,
            agent_name="PM",
            status="completed",
            task_description=f"Human review для задачи {task_id}: {pm_comment}",
            full_response=json.dumps({
                "human_prompt": human_prompt,
                "pm_decision": pm_decision
            }, ensure_ascii=False),
            tokens_used=pm_tokens
        )
        
        logger.info(f"✅ Human review для задачи {task_id}: {pm_comment}")
        
        return jsonify({
            "status": "success",
            "task_id": task_id,
            "pm_comment": pm_comment,
            "updated_description": updated_description
        })
        
    except Exception as e:
        logger.error(f"❌ Ошибка human review: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/api/nocodb', methods=['GET', 'POST', 'PATCH', 'DELETE'], strict_slashes=False)
@app.route('/api/nocodb/<path:path>', methods=['GET', 'POST', 'PATCH', 'DELETE'])
def nocodb_proxy(path=""):
    """Прокси для NocoDB API v3."""
    nocodb_url = Config.get_nocodb_records_url()
    if path:
        nocodb_url = f"{nocodb_url}/{path}"
    
    if request.args:
        allowed_params = ['limit', 'offset']
        filtered_params = {k: v for k, v in request.args.items() if k in allowed_params}
        if filtered_params:
            query_string = '&'.join([f"{k}={v}" for k, v in filtered_params.items()])
            nocodb_url += f"?{query_string}"
    
    headers = {"xc-token": Config.NOCODB_API_TOKEN, "Content-Type": "application/json"}
    
    try:
        resp = requests.request(
            method=request.method,
            url=nocodb_url,
            headers=headers,
            json=request.get_json(silent=True),
            timeout=30
        )
        return resp.content, resp.status_code, dict(resp.headers)
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка прокси в NocoDB: {e}")
        return jsonify({"error": str(e)}), 502


# ==================== ЗАПУСК ====================

def test_llm_connection():
    """Проверка подключения к Yandex AI Studio."""
    logger.info("🔍 Проверка подключения к Yandex AI Studio...")
    logger.info(f"   URL: {Config.get_llm_responses_url()}")
    logger.info(f"   Model: {Config.get_llm_model_uri()}")
    api_key_preview = f"{'*' * 8}{Config.LLM_API_KEY[-4:]}" if Config.LLM_API_KEY and len(Config.LLM_API_KEY) > 4 else "NOT SET"
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
    
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)