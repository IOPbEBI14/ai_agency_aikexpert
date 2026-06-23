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


# ==================== УТИЛИТЫ ====================

def load_prompt(agent_name: str) -> str:
    """Загружает системный промпт из файла prompts/{agent_name}_prompt.txt"""
    prompt_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        'prompts',
        f'{agent_name}_prompt.txt'
    )
    try:
        with open(prompt_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
            logger.info(f"📄 Загружен промпт для {agent_name} ({len(content)} символов)")
            return content
    except FileNotFoundError:
        logger.warning(f"⚠️ Промпт для {agent_name} не найден")
        return f"Ты — {agent_name}, агент ИИ-агентства. Выполняй задачу в формате JSON."
    except Exception as e:
        logger.error(f"❌ Ошибка чтения промпта {agent_name}: {e}")
        return f"Ты — {agent_name}. Выполняй задачу."


def call_llm(agent_name: str, system_prompt: str, user_task: str, max_retries: int = 2) -> tuple:
    """
    Вызов Yandex AI Studio через Responses API.
    Обрабатывает обрезанные ответы через retry.
    """
    url = Config.get_llm_responses_url()
    model_uri = Config.get_llm_model_uri()

    headers = {
        "Authorization": f"Bearer {Config.LLM_API_KEY}",
        "Content-Type": "application/json"
    }

    payload = {
        "model": model_uri,
        "instructions": system_prompt,
        "input": [{"role": "user", "content": user_task}],
        "temperature": 0.3,
        "max_tokens": 16000
    }

    last_exception = None
    
    for attempt in range(max_retries + 1):
        try:
            logger.info(f"🤖 Вызов агента: {agent_name} (попытка {attempt + 1}/{max_retries + 1})")
            response = requests.post(url, json=payload, headers=headers, timeout=180)

            if response.status_code != 200:
                error_msg = f"❌ LLM вернул статус {response.status_code}: {response.text[:300]}"
                logger.error(error_msg)
                
                # Если это последняя попытка — выбрасываем исключение
                if attempt == max_retries:
                    raise RuntimeError(error_msg)
                
                # Иначе продолжаем retry
                continue

            data = response.json()
            content = ""

            if "output_text" in data:
                content = data["output_text"]
            elif "output" in data:
                for item in data["output"]:
                    if item.get("type") == "message" and "content" in item:
                        for block in item["content"]:
                            if block.get("type") == "output_text":
                                content += block.get("text", "")

            # Проверка на обрезанный JSON
            content_stripped = content.strip()
            if content_stripped.startswith('{') and not content_stripped.endswith('}'):
                logger.warning(f"⚠️ Ответ {agent_name} обрезан. Попытка {attempt + 1}")
                if attempt < max_retries:
                    payload["max_tokens"] = min(payload["max_tokens"] * 2, 32000)
                    continue
                else:
                    content = try_fix_truncated_json(content)
                    if not content:
                        raise ValueError("Ответ обрезан и не может быть восстановлен")

            # Подсчёт токенов
            usage = data.get("usage", {})
            tokens = usage.get("total_tokens", 0)
            if tokens == 0:
                tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

            logger.info(f"✅ Агент {agent_name} ответил. Токенов: {tokens}")
            return content, tokens

        except requests.exceptions.Timeout as e:
            last_exception = e
            logger.error(f"⏱️ Таймаут вызова LLM для {agent_name}")
            if attempt < max_retries:
                continue
            raise
        except (RuntimeError, ValueError) as e:
            # Эти исключения пробрасываем дальше
            raise
        except Exception as e:
            last_exception = e
            logger.error(f"❌ Ошибка вызова LLM: {e}")
            if attempt < max_retries:
                continue
            raise

    raise RuntimeError(f"Не удалось получить ответ от {agent_name}") from last_exception

def try_fix_truncated_json(content: str) -> str:
    """Пытается восстановить обрезанный JSON."""
    if not content or not content.strip().startswith('{'):
        return ""

    content = content.strip()
    open_braces = content.count('{') - content.count('}')
    open_brackets = content.count('[') - content.count(']')

    fixed = content + ']' * open_brackets + '}' * open_braces

    try:
        json.loads(fixed)
        logger.info("🔧 Успешно восстановлен обрезанный JSON")
        return fixed
    except json.JSONDecodeError:
        last_comma = fixed.rfind(',')
        if last_comma > 0:
            fixed = fixed[:last_comma] + '}' * open_braces
            try:
                json.loads(fixed)
                return fixed
            except:
                pass
        return ""


def build_agent_task(task_description: str, input_data: dict, qa_feedback: str, iteration_count: int) -> str:
    """Формирует задачу для агента с учётом QA-фидбека."""
    task = f"""
ЗАДАЧА: {task_description}

ВХОДНЫЕ ДАННЫЕ (handoff от предыдущих задач):
{json.dumps(input_data, ensure_ascii=False, indent=2)}
"""

    if qa_feedback and iteration_count > 0:
        task += f"""

⚠️ ПРЕДЫДУЩАЯ ПРОВЕРКА QA НЕ ПРОШЛА. ИСПРАВЬ СЛЕДУЮЩЕЕ:
{qa_feedback}

ВАЖНО: Учти все замечания QA и верни ИСПРАВЛЕННЫЙ результат.
"""

    task += """

ИНСТРУКЦИЯ:
1. Выполнить задачу строго по описанию
2. Вернуть результат в формате JSON
3. Убедиться, что JSON полностью закрыт

Верни ТОЛЬКО валидный JSON.
"""
    return task


def log_to_agent_logs(project_id: int, agent_name: str, status: str,
                      task_description: str, full_response: str, tokens_used: int):
    """Универсальная функция логирования в agent_logs."""
    try:
        if not full_response or full_response.strip() == '':
            json_response = None
        else:
            try:
                json.loads(full_response)
                json_response = full_response
            except json.JSONDecodeError:
                json_response = json.dumps({
                    "content": full_response[:5000],
                    "type": "text",
                    "truncated": len(full_response) > 5000
                }, ensure_ascii=False)

        record = {
            "agent_name": agent_name,
            "status": status,
            "task_description": task_description[:2000] if task_description else "",
            "full_response": json_response,
            "tokens_used": tokens_used,
            "timestamp": datetime.now().isoformat()
        }

        if project_id is not None:
            record["project_id"] = project_id

        db.create_record(record)
        logger.debug(f"📝 Записано в agent_logs: {agent_name} ({status})")

    except Exception as e:
        logger.error(f"❌ Ошибка логирования в agent_logs: {e}", exc_info=True)


def update_last_agent_log(project_id: int, agent_name: str, new_status: str):
    """Обновляет статус последней записи агента в agent_logs."""
    try:
        recent = db.get_recent_records(limit=10)
        for log in recent:
            if log.get("agent_name") == agent_name and log.get("status") == "review":
                log_id = log.get("Id")
                if log_id:
                    payload = [{"id": log_id, "fields": {"status": new_status}}]
                    requests.patch(
                        Config.get_nocodb_records_url(),
                        json=payload,
                        headers={"xc-token": Config.NOCODB_API_TOKEN, "Content-Type": "application/json"},
                        timeout=30
                    )
                    return
    except Exception as e:
        logger.error(f"❌ Ошибка обновления записи agent_logs: {e}")


def validate_with_qa(agent_name: str, agent_response: str, task_description: str) -> Dict[str, Any]:
    """QA Gate: проверяет результат задачи через QA-агента."""
    logger.info(f"🔍 QA получает ответ от {agent_name}: {len(agent_response)} символов")
    
    qa_prompt = load_prompt("qa")
    
    qa_task = f"""
Ты — QA Agent. Проверь результат задачи.

ЗАДАЧА: {task_description}
АГЕНТ: {agent_name}

РЕЗУЛЬТАТ (длина: {len(agent_response)} символов):
{agent_response[:6000]}

ПРОВЕРЬ:
1. Соответствует ли результат задаче?
2. Нет ли ошибок или противоречий?
3. Достаточно ли данных для следующих задач?
4. Валиден ли JSON?

Верни JSON:
{{
    "approved": true/false,
    "feedback": "конкретные замечания если не прошло",
    "issues": ["список проблем"]
}}
"""
    
    try:
        qa_response, qa_tokens = call_llm("qa", qa_prompt, qa_task)
        qa_result = json.loads(qa_response)
        qa_result["tokens_used"] = qa_tokens
        
        # Убеждаемся, что есть обязательные ключи
        if "approved" not in qa_result:
            qa_result["approved"] = False
        if "feedback" not in qa_result:
            qa_result["feedback"] = ""
        if "issues" not in qa_result:
            qa_result["issues"] = []
        
        return qa_result
    except Exception as e:
        logger.error(f"❌ Ошибка QA: {e}")
        return {"approved": True, "feedback": "QA ошибка, пропускаем", "tokens_used": 0}
        
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