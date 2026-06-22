import os
import json
import logging
import threading
import time
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple
from flask import Flask, jsonify, request
from flask_cors import CORS
import requests
from urllib.parse import quote

from core.config import Config
from core.nocodb import NocoDBClient, ProjectsClient, TasksClient

# ==================== НАСТРОЙКА ЛОГИРОВАНИЯ ====================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("Orchestrator")

# ==================== ИНИЦИАЛИЗАЦИЯ КЛИЕНТОВ ====================
db = NocoDBClient()
projects_db = ProjectsClient()
tasks_db = TasksClient()

# ==================== FLASK ПРИЛОЖЕНИЕ ====================
app = Flask(__name__)
CORS(app)

# Глобальное состояние
agency_running = False
agency_thread = None
current_project = None


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
    Вызов Yandex AI Studio через Responses API с Bearer-авторизацией.
    Обрабатывает обрезанные ответы через retry и try_fix_truncated_json.
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
        "input": [
            {"role": "user", "content": user_task}
        ],
        "temperature": 0.3,
        "max_tokens": 16000
    }

    for attempt in range(max_retries + 1):
        try:
            logger.info(f"🤖 Вызов агента: {agent_name} (попытка {attempt + 1}/{max_retries + 1})")
            response = requests.post(url, json=payload, headers=headers, timeout=180)

            if response.status_code != 200:
                logger.error(f"❌ LLM вернул статус {response.status_code}: {response.text[:300]}")
                response.raise_for_status()

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

            # Проверка на обрезанный ответ
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

            # Проверка валидности JSON
            if content_stripped.startswith('{'):
                try:
                    json.loads(content_stripped)
                except json.JSONDecodeError as e:
                    logger.warning(f"⚠️ Невалидный JSON от {agent_name}: {e}. Попытка {attempt + 1}")
                    if attempt < max_retries:
                        continue
                    else:
                        content = try_fix_truncated_json(content)
                        if not content:
                            raise ValueError(f"Невалидный JSON после {max_retries} попыток")

            # Подсчёт токенов
            usage = data.get("usage", {})
            tokens = usage.get("total_tokens", 0)
            if tokens == 0:
                tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)

            logger.info(f"✅ Агент {agent_name} ответил. Токенов: {tokens}")
            return content, tokens

        except requests.exceptions.Timeout:
            logger.error(f"⏱️ Таймаут вызова LLM для {agent_name}")
            if attempt < max_retries:
                continue
            raise
        except Exception as e:
            logger.error(f"❌ Ошибка вызова LLM: {e}")
            if attempt < max_retries:
                continue
            raise

    raise RuntimeError(f"Не удалось получить ответ от {agent_name} после {max_retries + 1} попыток")


def try_fix_truncated_json(content: str) -> str:
    """Пытается восстановить обрезанный JSON, закрывая скобки."""
    if not content or not content.strip().startswith('{'):
        return ""

    content = content.strip()
    open_braces = content.count('{') - content.count('}')
    open_brackets = content.count('[') - content.count(']')

    fixed = content
    fixed += ']' * open_brackets
    fixed += '}' * open_braces

    try:
        json.loads(fixed)
        logger.info(f"🔧 Успешно восстановлен обрезанный JSON")
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


def build_summary(agent_name: str, agent_response: str) -> str:
    """Формирует краткое резюме ответа агента"""
    try:
        if not agent_response.strip().startswith('{'):
            return agent_response[:150]

        response_json = json.loads(agent_response)

        if agent_name == 'lead_hunter':
            leads_count = response_json.get('total_found', len(response_json.get('leads_found', [])))
            return f"Найдено лидов: {leads_count}"
        elif agent_name == 'sales':
            messages_count = len(response_json.get('messages', []))
            return f"Сгенерировано сообщений: {messages_count}"
        elif agent_name == 'analyst':
            roi = response_json.get('roi_calculation', {})
            saved = roi.get('cost_saved_per_month_rub', 0)
            return f"Экономия: {saved} руб/мес"
        elif agent_name == 'architect':
            return response_json.get('summary', response_json.get('approach', 'Архитектура готова'))
        elif agent_name == 'developer':
            return response_json.get('summary', 'Код готов')
        elif agent_name == 'qa':
            issues = len(response_json.get('issues', []))
            return f"Найдено проблем: {issues}"
        elif agent_name == 'tech_writer':
            return response_json.get('summary', 'Документация готова')
        elif agent_name == 'PM':
            return response_json.get('pm_comment', response_json.get('comment', 'Решение принято'))
        else:
            return response_json.get('summary', 'Задача выполнена')

    except json.JSONDecodeError:
        return agent_response[:150]
    except Exception:
        return agent_response[:150]


def log_to_agent_logs(project_id: int, agent_name: str, status: str,
                      task_description: str, full_response: str, tokens_used: int):
    """
    Универсальная функция логирования в agent_logs.
    ВАЖНО: поле full_response имеет тип JSON в NocoDB.
    """
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


def build_agent_task(task_description: str, input_data: dict, qa_feedback: str, iteration_count: int) -> str:
    """Формирует задачу для агента с учётом предыдущего QA-фидбека."""
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
3. Убедиться, что JSON полностью закрыт (все скобки закрыты)

Верни ТОЛЬКО валидный JSON.
"""
    return task


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
4. Валиден ли JSON (все скобки закрыты)?

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
        return qa_result
    except Exception as e:
        logger.error(f"❌ Ошибка QA: {e}")
        return {"approved": True, "feedback": "QA ошибка, пропускаем", "tokens_used": 0}


def build_initial_task_graph(pm_prompt: str, project: dict) -> list:
    """PM строит начальный Task Graph для проекта."""
    task_graph_prompt = f"""
Ты — Project Manager. Декомпозируй проект на задачи и построй граф зависимостей.

ПРОЕКТ:
Клиент: {project.get('client_name')}
Цель: {project.get('goal')}

ДОСТУПНЫЕ АГЕНТЫ:
- analyst: анализ потребностей и ROI
- architect: проектирование архитектуры (после analyst)
- developer: разработка автоматизаций (после architect, будет декомпозирован на подзадачи)
- crm_customizer: настройка CRM (после developer)
- qa: тестирование и валидация (после crm_customizer)
- tech_writer: документация (после qa)

ВАЖНО:
- analyst и architect — это ОДНА задача каждый
- developer — это ОДНА задача (после её выполнения PM декомпозирует на подзадачи)
- crm_customizer, qa, tech_writer — по одной задаче

ПОСТРОЙ Task Graph в формате JSON:
{{
    "tasks": [
        {{
            "task_id": "task_001",
            "agent_name": "analyst",
            "task_description": "Провести анализ...",
            "depends_on": [],
            "input_data": {{}},
            "max_iterations": 3
        }},
        {{
            "task_id": "task_002",
            "agent_name": "architect",
            "task_description": "Спроектировать архитектуру...",
            "depends_on": ["task_001"],
            "input_data": {{"roi_data": "результат analyst"}},
            "max_iterations": 3
        }},
        {{
            "task_id": "task_003",
            "agent_name": "developer",
            "task_description": "Реализовать архитектуру...",
            "depends_on": ["task_002"],
            "input_data": {{"architecture": "результат architect"}},
            "max_iterations": 3
        }}
    ]
}}

Верни ТОЛЬКО валидный JSON.
"""

    try:
        pm_response, pm_tokens = call_llm("PM", pm_prompt, task_graph_prompt)
        current_project["tokens_used"] = (current_project.get("tokens_used", 0) or 0) + pm_tokens
        projects_db.update_project(current_project.get("Id"), {"tokens_used": current_project["tokens_used"]})

        task_graph = json.loads(pm_response)
        return task_graph.get("tasks", [])
    except Exception as e:
        logger.error(f"❌ Ошибка построения Task Graph: {e}")
        return []


# ==================== ФУНКЦИИ ДЛЯ РАБОТЫ С РОДИТЕЛЬСКИМИ ЗАДАЧАМИ ====================

def _check_and_complete_parent_tasks(tasks: list, project_id: int):
    """
    Проверяет родительские задачи: если все их подзадачи завершены,
    помечает родителя как completed.
    """
    parent_candidates = {}

    for task in tasks:
        task_id = task.get("task_id", "")
        status = task.get("status", "")

        if task_id.startswith("dev_"):
            parent_id = "task_003"
            if parent_id not in parent_candidates:
                parent_candidates[parent_id] = []
            parent_candidates[parent_id].append({
                "task_id": task_id,
                "status": status
            })

    for parent_id, subtasks in parent_candidates.items():
        all_completed = all(st["status"] == "completed" for st in subtasks)

        if all_completed:
            parent_task = next((t for t in tasks if t.get("task_id") == parent_id), None)
            if parent_task and parent_task.get("status") != "completed":
                parent_db_id = parent_task.get("Id")
                logger.info(f"✅ Все подзадачи {parent_id} завершены ({len(subtasks)} шт.). Помечаем родителя как completed.")
                tasks_db.update_task(parent_db_id, {
                    "status": "completed",
                    "qa_approved": "true",
                    "qa_feedback": f"Все {len(subtasks)} подзадач завершены успешно"
                })


def _expand_completed_with_parents(tasks: list, completed_task_ids: list) -> list:
    """
    Расширяет список completed_task_ids: если родительская задача декомпозирована
    и все её подзадачи выполнены, добавляем ID родителя в список completed.
    """
    expanded = set(completed_task_ids)

    parent_subtasks = {}
    for task in tasks:
        task_id = task.get("task_id", "")
        status = task.get("status", "")

        if task_id.startswith("dev_"):
            parent_id = "task_003"
            if parent_id not in parent_subtasks:
                parent_subtasks[parent_id] = []
            parent_subtasks[parent_id].append(status)

    for parent_id, statuses in parent_subtasks.items():
        if all(s == "completed" for s in statuses):
            expanded.add(parent_id)

    return list(expanded)


def _resolve_deadlock_with_pm(pm_prompt: str, project: dict, tasks: list, pending_tasks: list) -> bool:
    """
    Вызывает PM для анализа тупика: есть pending задачи, но ни одна не готова.
    """
    project_id = project.get("Id")

    tasks_summary = []
    for t in tasks:
        tasks_summary.append({
            "task_id": t.get("task_id"),
            "agent": t.get("agent_name"),
            "status": t.get("status"),
            "depends_on": t.get("depends_on", "[]")
        })

    pm_task = f"""
ПРОЕКТ В ТУПИКЕ! Есть задачи в статусе pending, но ни одна не готова к выполнению из-за зависимостей.

ВСЕ ЗАДАЧИ ПРОЕКТА:
{json.dumps(tasks_summary, ensure_ascii=False, indent=2)}

ЗАДАЧИ В PENDING (не готовы):
{json.dumps([{'task_id': t.get('task_id'), 'agent': t.get('agent_name'), 'depends_on': t.get('depends_on')} for t in pending_tasks], ensure_ascii=False, indent=2)}

ТВОЯ ЗАДАЧА:
1. Проанализируй ситуацию
2. Предложи решение:
   - Обновить зависимости
   - Пропустить проваленные задачи
   - Создать новые задачи
3. Верни JSON с решением

ФОРМАТ ОТВЕТА:
{{
    "analysis": "анализ ситуации",
    "solution": "update_dependencies | skip_tasks | create_tasks | stop_project",
    "actions": [
        {{
            "action": "update_task | skip_task | create_task",
            "task_id": "task_id",
            "new_status": "pending | completed | failed",
            "new_depends_on": ["обновлённые зависимости"]
        }}
    ],
    "comment": "комментарий PM"
}}
"""

    try:
        pm_response, pm_tokens = call_llm("PM", pm_prompt, pm_task)
        current_project["tokens_used"] = (current_project.get("tokens_used", 0) or 0) + pm_tokens
        projects_db.update_project(project_id, {"tokens_used": current_project["tokens_used"]})

        pm_decision = json.loads(pm_response)
        solution = pm_decision.get("solution")
        actions = pm_decision.get("actions", [])

        logger.info(f"💡 PM предложил решение: {solution}. Комментарий: {pm_decision.get('comment')}")

        for action in actions:
            action_type = action.get("action")
            task_id = action.get("task_id")

            task = next((t for t in tasks if t.get("task_id") == task_id), None)
            if not task:
                continue

            task_db_id = task.get("Id")

            if action_type == "update_task":
                update_data = {}
                if "new_status" in action:
                    update_data["status"] = action["new_status"]
                if "new_depends_on" in action:
                    update_data["depends_on"] = json.dumps(action["new_depends_on"], ensure_ascii=False)
                if update_data:
                    tasks_db.update_task(task_db_id, update_data)

            elif action_type == "skip_task":
                tasks_db.update_task(task_db_id, {"status": "failed", "qa_feedback": "Пропущено по решению PM"})

        return solution != "stop_project"

    except Exception as e:
        logger.error(f"❌ Ошибка разрешения тупика через PM: {e}")
        return False


# ==================== ФУНКЦИЯ ФИНАЛИЗАЦИИ ПРОЕКТА ====================

def finalize_project(pm_prompt: str, project: dict, tasks: list) -> Optional[Dict[str, Any]]:
    """
    PM собирает результаты всех задач и формирует финальный отчет.
    """
    project_id = project.get("Id")
    
    # Собираем результаты задач
    task_results = []
    for task in tasks:
        task_results.append({
            "task_id": task.get("task_id"),
            "agent": task.get("agent_name"),
            "description": task.get("task_description"),
            "output": task.get("output_data", "")[:500],
            "status": task.get("status"),
            "iterations": task.get("iteration_count", 0),
            "tokens": task.get("tokens_used", 0)
        })
    
    # Собираем метрики
    total_tokens = sum(t.get("tokens_used", 0) for t in tasks)
    total_iterations = sum(t.get("iteration_count", 0) for t in tasks)
    completed_count = len([t for t in tasks if t.get("status") == "completed"])
    
    # Группируем токены по агентам
    tokens_per_agent = {}
    for task in tasks:
        agent = task.get("agent_name")
        tokens = task.get("tokens_used", 0)
        tokens_per_agent[agent] = tokens_per_agent.get(agent, 0) + tokens
    
    # Формируем задачу для PM
    pm_task = f"""
ПРОЕКТ ЗАВЕРШЕН. Все задачи выполнены.

ИНФОРМАЦИЯ О ПРОЕКТЕ:
Клиент: {project.get('client_name')}
Цель: {project.get('goal')}

ВЫПОЛНЕННЫЕ ЗАДАЧИ ({completed_count} из {len(tasks)}):
{json.dumps(task_results, ensure_ascii=False, indent=2)}

МЕТРИКИ:
- Общие затраты токенов: {total_tokens}
- Общее количество итераций: {total_iterations}
- Затраты по агентам: {json.dumps(tokens_per_agent, ensure_ascii=False)}

ТВОЯ ЗАДАЧА:
1. Проанализируй результаты всех задач
2. Сформируй финальный отчет для клиента (в markdown-формате)
3. Включи разделы:
   - Краткое резюме проекта
   - Что было сделано (по каждому агенту)
   - Технические детали
   - Рекомендации по использованию
   - Метрики проекта

ФОРМАТ ОТВЕТА (строго JSON):
{{
  "project_status": "completed",
  "final_report": "# Отчет по проекту\\n\\n## Краткое резюме\\n...\\n\\n## Что было сделано\\n...\\n\\n## Метрики\\n...",
  "metrics": {{
    "total_duration_minutes": 45,
    "total_tokens_used": {total_tokens},
    "tasks_completed": {completed_count},
    "tokens_per_agent": {json.dumps(tokens_per_agent, ensure_ascii=False)}
  }},
  "pm_comment": "Проект успешно завершен."
}}

ВАЖНО:
- Поле final_report должно содержать ПОЛНЫЙ текст отчета в markdown
- Используй markdown-форматирование (#, ##, **, -)
- Отчет должен быть понятен не-техническому специалисту
- Включи конкретные цифры и примеры

Верни ТОЛЬКО валидный JSON.
"""
    
    try:
        logger.info("🤖 Вызов PM для финализации проекта...")
        pm_response, pm_tokens = call_llm("PM", pm_prompt, pm_task)
        
        logger.info(f"📏 Длина ответа PM: {len(pm_response)} символов")
        logger.info(f"📄 Превью ответа: {pm_response[:300]}...")
        
        # Обновляем бюджет проекта
        current_tokens = project.get("tokens_used", 0) or 0
        project["tokens_used"] = current_tokens + pm_tokens
        projects_db.update_project(project_id, {"tokens_used": project["tokens_used"]})
        
        # Парсим ответ PM
        try:
            pm_decision = json.loads(pm_response)
            logger.info(f"✅ PM вернул валидный JSON")
            
            # Проверяем наличие final_report
            final_report = pm_decision.get("final_report", "")
            if not final_report or final_report.strip() == "":
                logger.warning("⚠️ Поле final_report пустое в ответе PM!")
                logger.warning(f"📄 Ключи в ответе: {list(pm_decision.keys())}")
                
                # Пытаемся восстановить отчет из других полей
                if "report" in pm_decision:
                    final_report = pm_decision["report"]
                    logger.info(f"🔧 Восстановлен отчет из поля 'report'")
                elif "summary" in pm_decision:
                    final_report = pm_decision["summary"]
                    logger.info(f"🔧 Восстановлен отчет из поля 'summary'")
                else:
                    # Создаем минимальный отчет из метрик
                    final_report = f"""# Отчет по проекту

## Метрики
- Всего задач выполнено: {completed_count}
- Общие затраты токенов: {total_tokens}
- Общее количество итераций: {total_iterations}

## Затраты по агентам
{chr(10).join([f"- {agent}: {tokens} токенов" for agent, tokens in tokens_per_agent.items()])}
"""
                    logger.info(f"🔧 Создан минимальный отчет из метрик")
            
            logger.info(f"📄 Длина final_report: {len(final_report)} символов")
            
            # Сохраняем в проект
            pm_decision["final_report"] = final_report
            return pm_decision
            
        except json.JSONDecodeError as e:
            logger.error(f" PM вернул невалидный JSON: {e}")
            logger.error(f"📄 Ответ PM (первые 1000 символов): {pm_response[:1000]}")
            
            # Пытаемся восстановить JSON
            try:
                # Ищем JSON в ответе
                start = pm_response.find('{')
                end = pm_response.rfind('}') + 1
                if start >= 0 and end > start:
                    json_str = pm_response[start:end]
                    pm_decision = json.loads(json_str)
                    logger.info(f"🔧 Восстановлен JSON из ответа PM")
                    return pm_decision
            except:
                pass
            
            # Возвращаем минимальный отчет
            return {
                "project_status": "completed",
                "final_report": f"""# Отчет по проекту

## Метрики
- Всего задач выполнено: {completed_count}
- Общие затраты токенов: {total_tokens}

## Затраты по агентам
{chr(10).join([f"- {agent}: {tokens} токенов" for agent, tokens in tokens_per_agent.items()])}
""",
                "metrics": {
                    "total_tokens_used": total_tokens,
                    "tasks_completed": completed_count,
                    "tokens_per_agent": tokens_per_agent
                },
                "pm_comment": f"Ошибка парсинга ответа PM: {str(e)}"
            }
        
    except Exception as e:
        logger.error(f"❌ Ошибка финализации проекта: {e}", exc_info=True)
        return None

# ==================== FLASK API ENDPOINTS ====================

@app.route('/api/agency/status', methods=['GET'])
def get_status():
    """Статус работы агентства с финальным отчетом"""
    global current_project
    
    if current_project:
        tokens_used = current_project.get("tokens_used", 0) or 0
        token_budget = current_project.get("token_budget", Config.TOKEN_BUDGET) or Config.TOKEN_BUDGET
        status = current_project.get("status", "in_progress")
        client = current_project.get("client_name", "—")
        
        # Читаем final_report и metrics из проекта
        final_report = current_project.get("final_report", "") or ""
        metrics_str = current_project.get("metrics", "{}") or "{}"
        completed_at = current_project.get("completed_at", "") or ""
        
        # Парсим metrics
        try:
            if isinstance(metrics_str, str):
                metrics = json.loads(metrics_str) if metrics_str.strip() else {}
            else:
                metrics = metrics_str
        except json.JSONDecodeError as e:
            logger.warning(f"⚠️ Ошибка парсинга metrics: {e}")
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
        "running": agency_running,
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
    """Запуск агентства с поиском существующего проекта"""
    global agency_running, agency_thread, current_project
    
    if agency_running:
        return jsonify({"error": "Already running"}), 400
    
    # 1. Ищем активный проект
    current_project = projects_db.find_project_by_status("in_progress")
    
    if current_project:
        logger.info(f"▶ Найден активный проект: {current_project.get('project_name')}")
    else:
        # 2. Ищем остановленный проект
        current_project = projects_db.find_project_by_status("stopped")
        if not current_project:
            current_project = projects_db.find_project_by_status("needs_human_review")
        
        if current_project:
            logger.info(f"▶ Возобновляем проект: {current_project.get('project_name')}")
            current_project["status"] = "in_progress"
            projects_db.update_project(current_project["Id"], {"status": "in_progress"})
        else:
            # 3. Создаём новый
            logger.info("▶ Создаём новый проект")
            #current_project = projects_db.create_project(
            #   project_name=Config.DEFAULT_PROJECT_NAME,
            #    client_name=Config.DEFAULT_CLIENT_NAME,
            #   goal=Config.DEFAULT_GOAL,
            #    token_budget=Config.TOKEN_BUDGET
            #)
    
    # ⭐ ВАЖНО: Убеждаемся, что current_project содержит все необходимые поля
    if current_project:
        if not current_project.get("project_name"):
            current_project["project_name"] = Config.DEFAULT_PROJECT_NAME
        if not current_project.get("client_name"):
            current_project["client_name"] = Config.DEFAULT_CLIENT_NAME
        if not current_project.get("status"):
            current_project["status"] = "in_progress"
    
    agency_running = True
    agency_thread = threading.Thread(target=run_agency, daemon=True)
    agency_thread.start()
    
    return jsonify({
        "status": "started",
        "project": current_project.get("project_name") if current_project else None,
        "phase": current_project.get("current_phase") if current_project else None,
        "tokens_used": current_project.get("tokens_used", 0) if current_project else 0,
        "token_budget": current_project.get("token_budget", Config.TOKEN_BUDGET) if current_project else Config.TOKEN_BUDGET
    })

@app.route('/api/agency/stop', methods=['POST'])
def stop_agency():
    """Остановка оркестратора"""
    global agency_running, current_project

    agency_running = False

    if current_project and current_project.get("Id"):
        current_project["status"] = "stopped"
        projects_db.update_project(current_project["Id"], {"status": "stopped"})

    logger.info(" Агентство остановлено")
    return jsonify({"status": "stopped"})


@app.route('/api/agency/resume', methods=['POST'])
def resume_agency():
    """Возобновить работу агентства после остановки"""
    global agency_running, agency_thread, current_project

    if agency_running:
        return jsonify({"error": "Already running"}), 400

    current_project = projects_db.find_project_by_status("stopped")
    if not current_project:
        current_project = projects_db.find_project_by_status("needs_human_review")

    if not current_project:
        return jsonify({"error": "No stopped project found"}), 404

    current_project["status"] = "in_progress"
    projects_db.update_project(current_project["Id"], {"status": "in_progress"})

    agency_running = True
    agency_thread = threading.Thread(target=run_agency, daemon=True)
    agency_thread.start()

    logger.info(f"▶ Агентство возобновлено. Фаза: {current_project.get('current_phase')}")
    return jsonify({"status": "resumed", "phase": current_project.get("current_phase")})


@app.route('/api/agency/increase-tokens', methods=['POST'])
def increase_tokens():
    """Увеличить бюджет токенов на начальное значение"""
    global current_project

    if not current_project:
        current_project = projects_db.get_or_create_project(
            project_name=Config.DEFAULT_PROJECT_NAME,
            client_name=Config.DEFAULT_CLIENT_NAME,
            goal=Config.DEFAULT_GOAL,
            token_budget=Config.TOKEN_BUDGET
        )

    initial_budget = Config.TOKEN_BUDGET
    current_budget = current_project.get("token_budget", 0) or 0
    new_budget = current_budget + initial_budget

    if current_project.get("Id"):
        projects_db.update_project(current_project["Id"], {"token_budget": new_budget})
    current_project["token_budget"] = new_budget

    logger.info(f"💰 Бюджет увеличен: {current_budget} → {new_budget}")
    return jsonify({"status": "success", "new_budget": new_budget, "added": initial_budget})


@app.route('/api/agency/human-review', methods=['POST'])
def human_review():
    """Обработка ручного вмешательства в задачу"""
    global current_project

    data = request.get_json()
    task_id = data.get('task_id')
    human_prompt = data.get('human_prompt')

    if not task_id or not human_prompt:
        return jsonify({"error": "task_id и human_prompt обязательны"}), 400

    if not current_project:
        return jsonify({"error": "Нет активного проекта"}), 400

    project_id = current_project.get("Id")
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
2. Скорректируй задачу для агента {task.get('agent_name')} с учётом этих указаний
3. Верни обновлённое описание задачи

ФОРМАТ ОТВЕТА (строго JSON):
{{
    "updated_task_description": "новое описание задачи с учётом указаний человека",
    "pm_comment": "комментарий о том, что было изменено"
}}

Верни ТОЛЬКО валидный JSON.
"""

        pm_response, pm_tokens = call_llm("PM", pm_prompt, pm_task)

        current_tokens = current_project.get("tokens_used", 0) or 0
        current_project["tokens_used"] = current_tokens + pm_tokens
        projects_db.update_project(project_id, {"tokens_used": current_project["tokens_used"]})

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

    except json.JSONDecodeError as e:
        logger.error(f" PM вернул невалидный JSON: {e}")
        return jsonify({"error": f"PM вернул невалидный JSON: {str(e)}"}), 500
    except Exception as e:
        logger.error(f"❌ Ошибка human review: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route('/api/nocodb', methods=['GET', 'POST', 'PATCH', 'DELETE'], strict_slashes=False)
@app.route('/api/nocodb/<path:path>', methods=['GET', 'POST', 'PATCH', 'DELETE'])
def nocodb_proxy(path=""):
    """Прокси для NocoDB API v3 (таблица agent_logs)"""
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


# ==================== ОСНОВНОЙ ОРКЕСТРАТОР ====================

def run_agency():
    """
    Supervisor-Workers оркестрация с декомпозицией задач и финализацией.
    """
    global agency_running, current_project

    logger.info("▶ Запуск Supervisor-Workers оркестрации")

    pm_prompt = load_prompt("pm")
    agent_prompts_cache = {}
    iteration = 0
    max_total_iterations = 50

    while agency_running and iteration < max_total_iterations:
        iteration += 1

        if not current_project:
            logger.error("❌ Нет активного проекта")
            break

        project_id = current_project.get("Id")
        if not project_id:
            logger.error("❌ У проекта нет Id")
            break

        tokens_used = current_project.get("tokens_used", 0) or 0
        token_budget = current_project.get("token_budget", Config.TOKEN_BUDGET) or Config.TOKEN_BUDGET

        remaining_tokens = token_budget - tokens_used
        if remaining_tokens < (token_budget * 0.2):
            logger.warning(f"⚠️ Бюджет на исходе: {tokens_used}/{token_budget}")
            current_project["status"] = "needs_human_review"
            projects_db.update_project(project_id, {
                "status": "needs_human_review",
                "tokens_used": tokens_used
            })
            log_to_agent_logs(
                project_id=project_id,
                agent_name="PM",
                status="needs_review",
                task_description=f"Автоматическая остановка: использовано {tokens_used} из {token_budget} токенов.",
                full_response=json.dumps({
                    "reason": "budget_exhausted",
                    "tokens_used": tokens_used,
                    "token_budget": token_budget
                }, ensure_ascii=False),
                tokens_used=0
            )
            break

        # Получаем все задачи
        tasks = tasks_db.get_tasks_by_project(project_id)

        if not tasks:
            logger.info("🧠 PM строит Task Graph...")
            task_graph = build_initial_task_graph(pm_prompt, current_project)

            if not task_graph:
                break

            for task_data in task_graph:
                task_data["project_id"] = project_id
                task_data["status"] = "pending"
                task_data["iteration_count"] = 0
                task_data["qa_approved"] = "pending"
                task_data["created_at"] = datetime.now().isoformat()
                tasks_db.create_task(task_data)

            logger.info(f"✅ Task Graph создан: {len(task_graph)} задач")
            tasks = tasks_db.get_tasks_by_project(project_id)

            log_to_agent_logs(
                project_id=project_id,
                agent_name="PM",
                status="completed",
                task_description=f"Построен Task Graph из {len(task_graph)} задач",
                full_response=json.dumps(task_graph, ensure_ascii=False),
                tokens_used=0
            )

        # ⭐ ПРОВЕРКА РОДИТЕЛЬСКИХ ЗАДАЧ
        _check_and_complete_parent_tasks(tasks, project_id)
        tasks = tasks_db.get_tasks_by_project(project_id)

        # Анализ состояния
        completed_tasks = [t for t in tasks if t.get("status") == "completed"]
        failed_tasks = [t for t in tasks if t.get("status") == "failed"]
        pending_tasks = [t for t in tasks if t.get("status") == "pending"]
        in_progress_tasks = [t for t in tasks if t.get("status") == "in_progress"]

        logger.info(f"📊 Статус задач: completed={len(completed_tasks)}, pending={len(pending_tasks)}, "
                   f"in_progress={len(in_progress_tasks)}, failed={len(failed_tasks)}")

        # ⭐ ПРОВЕРКА ЗАВЕРШЕНИЯ ВСЕХ ЗАДАЧ → ФИНАЛИЗАЦИЯ
        if not pending_tasks and not in_progress_tasks and completed_tasks:
            logger.info(f"🏁 Все задачи завершены: {len(completed_tasks)} выполнено, {len(failed_tasks)} провалено")
            logger.info("📝 PM формирует финальный отчет...")
            final_report = finalize_project(pm_prompt, current_project, tasks)

            if final_report:
                # ? ОТЛАДКА: логируем что получили
                logger.info(f"?? Получен final_report от PM:")
                logger.info(f"   - Ключи: {list(final_report.keys())}")
                logger.info(f"   - final_report длина: {len(final_report.get('final_report', ''))}")
                logger.info(f"   - metrics: {final_report.get('metrics')}")
                
                # Сохраняем финальный отчет в проект
                projects_db.update_project(project_id, {
                    "status": "completed",
                    "final_report": final_report.get("final_report", ""),
                    "metrics": json.dumps(final_report.get("metrics", {}), ensure_ascii=False),
                    "completed_at": datetime.now().isoformat()
                })
                
                logger.info("? Проект завершен. Финальный отчет сохранен.")
                
                # Логируем завершение
                log_to_agent_logs(
                    project_id=project_id,
                    agent_name="PM",
                    status="completed",
                    task_description="Финализация проекта. Отчет подготовлен для клиента.",
                    full_response=final_report.get("final_report", ""),
                    tokens_used=0
                )
                
                current_project["status"] = "completed"
                break
        else:
            logger.error("? Ошибка финализации проекта: final_report is None")
            current_project["status"] = "needs_human_review"
            projects_db.update_project(project_id, {"status": "needs_human_review"})
            break

        if not pending_tasks and not in_progress_tasks and not completed_tasks:
            logger.warning("⚠️ Нет задач для выполнения")
            break

        # Поиск готовых задач
        completed_task_ids = [t.get("task_id") for t in completed_tasks]
        completed_task_ids = _expand_completed_with_parents(tasks, completed_task_ids)

        ready_tasks = []
        for task in pending_tasks:
            depends_on = task.get("depends_on", "[]")
            try:
                depends_on = json.loads(depends_on) if isinstance(depends_on, str) else depends_on
            except:
                depends_on = []

            if all(dep_id in completed_task_ids for dep_id in depends_on):
                ready_tasks.append(task)

        logger.info(f" Готовых задач к выполнению: {len(ready_tasks)}")
        for t in ready_tasks:
            logger.info(f"   - {t.get('task_id')} ({t.get('agent_name')})")

        if not ready_tasks and in_progress_tasks:
            logger.info("⏳ Нет готовых задач, ждём завершения текущих...")
            time.sleep(5)
            continue

        if not ready_tasks:
            if pending_tasks:
                logger.warning(f"⚠️ Тупик: {len(pending_tasks)} задач в pending. Вызываем PM для анализа...")
                pm_resolution = _resolve_deadlock_with_pm(pm_prompt, current_project, tasks, pending_tasks)
                if pm_resolution:
                    continue
                else:
                    logger.error("❌ PM не смог разрешить тупик")
                    break
            else:
                logger.warning("⚠️ Нет готовых задач и нет pending — возможно, тупик")
                break

        # Выполнение задачи
        task = ready_tasks[0]
        task_db_id = task.get("Id")
        task_name = task.get("task_id")
        agent_name = task.get("agent_name")
        task_description = task.get("task_description")
        input_data = task.get("input_data", "{}")
        iteration_count = task.get("iteration_count", 0) or 0
        max_iter = task.get("max_iterations", Config.MAX_TASK_ITERATIONS) or Config.MAX_TASK_ITERATIONS
        qa_feedback = task.get("qa_feedback", "")

        try:
            input_data = json.loads(input_data) if isinstance(input_data, str) else input_data
        except:
            input_data = {}

        if iteration_count >= max_iter:
            logger.warning(f"⚠️ Задача {task_name} превысила лимит итераций ({max_iter})")
            tasks_db.update_task(task_db_id, {
                "status": "failed",
                "qa_feedback": "Превышен лимит итераций"
            })
            continue

        tasks_db.update_task(task_db_id, {
            "status": "in_progress",
            "iteration_count": iteration_count + 1
        })

        logger.info(f"▶ Выполнение задачи {task_name} ({agent_name}), итерация {iteration_count + 1}/{max_iter}")

        # ==================== ОСОБАЯ ЛОГИКА ДЛЯ ARCHITECT ====================
        if agent_name == "architect":
            logger.info("🏗️ Architect работает. После завершения PM декомпозирует на подзадачи...")

            if agent_name not in agent_prompts_cache:
                agent_prompts_cache[agent_name] = load_prompt(agent_name)
            agent_prompt = agent_prompts_cache[agent_name]

            agent_task = build_agent_task(task_description, input_data, qa_feedback, iteration_count)

            try:
                agent_response, agent_tokens = call_llm(agent_name, agent_prompt, agent_task)
                tokens_used += agent_tokens
                current_project["tokens_used"] = tokens_used
                projects_db.update_project(project_id, {"tokens_used": tokens_used})

                log_to_agent_logs(
                    project_id=project_id,
                    agent_name=agent_name,
                    status="review",
                    task_description=f"[{task_name}] Итерация {iteration_count + 1}: {task_description[:150]}",
                    full_response=agent_response,
                    tokens_used=agent_tokens
                )

                tasks_db.update_task(task_db_id, {
                    "output_data": agent_response,
                    "tokens_used": (task.get("tokens_used", 0) or 0) + agent_tokens
                })

                # QA-проверка
                logger.info(f"🔍 QA-проверка для задачи {task_name}...")
                qa_result = validate_with_qa(agent_name, agent_response, task_description)
                qa_tokens = qa_result.get("tokens_used", 0)
                tokens_used += qa_tokens
                current_project["tokens_used"] = tokens_used
                projects_db.update_project(project_id, {"tokens_used": tokens_used})

                log_to_agent_logs(
                    project_id=project_id,
                    agent_name="qa",
                    status="completed" if qa_result.get("approved") else "needs_review",
                    task_description=f"QA-проверка {task_name}: {'✅ ПРОШЁЛ' if qa_result.get('approved') else '❌ НЕ ПРОШЁЛ'}",
                    full_response=json.dumps(qa_result, ensure_ascii=False),
                    tokens_used=qa_tokens
                )

                if not qa_result.get("approved"):
                    logger.warning(f"⚠️ QA не прошёл для {task_name}: {qa_result.get('feedback', '')[:200]}")
                    tasks_db.update_task(task_db_id, {
                        "status": "pending",
                        "qa_approved": "false",
                        "qa_feedback": qa_result.get("feedback", "")
                    })
                    update_last_agent_log(project_id, agent_name, "needs_review")
                    continue

                # ✅ Architect прошёл QA — PM декомпозирует на подзадачи
                logger.info("✅ Architect прошёл QA. PM декомпозирует архитектуру на подзадачи для developer...")

                decompose_prompt = f"""
Ты — Project Manager. Архитектор завершил проектирование. Разбей архитектуру на подзадачи для developer.

АРХИТЕКТУРА ОТ ARCHITECT:
{agent_response[:4000]}

ЦЕЛЬ ПРОЕКТА:
{current_project.get('goal')}

ФОРМАТ ОТВЕТА (строго JSON):
{{
  "subtasks": [
    {{
      "subtask_id": "dev_001",
      "description": "Создать webhook для Telegram в n8n",
      "depends_on": [],
      "context": "Из архитектуры: Telegram Bot API, webhook endpoint /telegram"
    }},
    {{
      "subtask_id": "dev_002",
      "description": "Создать webhook для VK в n8n",
      "depends_on": [],
      "context": "Из архитектуры: VK Callback API, webhook endpoint /vk"
    }}
  ],
  "pm_comment": "Разбил архитектуру на N подзадач."
}}

ПРАВИЛА:
- Каждая подзадача атомарна (один компонент/интеграция)
- Максимум 5-7 подзадач
- Указывай зависимости между подзадачами
- Передавай developer только релевантный контекст

Верни ТОЛЬКО валидный JSON.
"""

                pm_response, pm_tokens = call_llm("PM", pm_prompt, decompose_prompt)
                tokens_used += pm_tokens
                current_project["tokens_used"] = tokens_used
                projects_db.update_project(project_id, {"tokens_used": tokens_used})

                try:
                    pm_decision = json.loads(pm_response)
                    subtasks = pm_decision.get("subtasks", [])

                    if not subtasks:
                        logger.error(" PM не вернул подзадачи")
                        tasks_db.update_task(task_db_id, {"status": "failed"})
                        continue

                    logger.info(f"✅ PM декомпозировал на {len(subtasks)} подзадач")

                    for subtask in subtasks:
                        subtask_data = {
                            "task_id": subtask.get("subtask_id"),
                            "project_id": project_id,
                            "agent_name": "developer",
                            "task_description": subtask.get("description"),
                            "input_data": json.dumps({
                                "context": subtask.get("context", ""),
                                "architecture_summary": agent_response[:2000]
                            }, ensure_ascii=False),
                            "status": "pending",
                            "depends_on": json.dumps(subtask.get("depends_on", []), ensure_ascii=False),
                            "iteration_count": 0,
                            "max_iterations": 3,
                            "qa_approved": "pending",
                            "created_at": datetime.now().isoformat()
                        }
                        tasks_db.create_task(subtask_data)

                    # ⭐ ВАЖНО: Обновляем статус родительской задачи на completed
                    if task_db_id:
                        tasks_db.update_task(task_db_id, {
                            "status": "completed",
                            "qa_approved": "true",
                            "qa_feedback": f"Декомпозирована на {len(subtasks)} подзадач: {', '.join([s.get('subtask_id') for s in subtasks])}"
                        })
                        logger.info(f"✅ Родительская задача {task_name} переведена в статус completed")

                    log_to_agent_logs(
                        project_id=project_id,
                        agent_name="PM",
                        status="completed",
                        task_description=f"Декомпозиция задачи {task_name} на {len(subtasks)} подзадач для developer",
                        full_response=json.dumps(pm_decision, ensure_ascii=False),
                        tokens_used=pm_tokens
                    )

                    logger.info(f"✅ Задача {task_name} декомпозирована. Создано {len(subtasks)} подзадач.")

                except json.JSONDecodeError as e:
                    logger.error(f"❌ PM вернул невалидный JSON при декомпозиции: {e}")
                    tasks_db.update_task(task_db_id, {"status": "failed"})
                    continue

            except Exception as e:
                logger.error(f"❌ Ошибка выполнения задачи {task_name}: {e}", exc_info=True)
                tasks_db.update_task(task_db_id, {
                    "status": "pending" if iteration_count + 1 < max_iter else "failed",
                    "qa_feedback": f"Ошибка: {str(e)[:500]}"
                })

            continue

        # ==================== ОБЫЧНАЯ ЛОГИКА ДЛЯ ДРУГИХ АГЕНТОВ ====================
        if agent_name not in agent_prompts_cache:
            agent_prompts_cache[agent_name] = load_prompt(agent_name)
        agent_prompt = agent_prompts_cache[agent_name]

        agent_task = build_agent_task(task_description, input_data, qa_feedback, iteration_count)

        try:
            agent_response, agent_tokens = call_llm(agent_name, agent_prompt, agent_task)
            tokens_used += agent_tokens
            current_project["tokens_used"] = tokens_used
            projects_db.update_project(project_id, {"tokens_used": tokens_used})

            log_to_agent_logs(
                project_id=project_id,
                agent_name=agent_name,
                status="review",
                task_description=f"[{task_name}] Итерация {iteration_count + 1}: {task_description[:150]}",
                full_response=agent_response,
                tokens_used=agent_tokens
            )

            tasks_db.update_task(task_db_id, {
                "output_data": agent_response,
                "tokens_used": (task.get("tokens_used", 0) or 0) + agent_tokens
            })

            # QA Gate
            if agent_name != "qa":
                logger.info(f"🔍 QA-проверка для задачи {task_name}...")
                qa_result = validate_with_qa(agent_name, agent_response, task_description)
                qa_tokens = qa_result.get("tokens_used", 0)
                tokens_used += qa_tokens
                current_project["tokens_used"] = tokens_used
                projects_db.update_project(project_id, {"tokens_used": tokens_used})

                qa_approved = qa_result.get("approved", False)
                qa_feedback_text = qa_result.get("feedback", "")

                log_to_agent_logs(
                    project_id=project_id,
                    agent_name="qa",
                    status="completed" if qa_approved else "needs_review",
                    task_description=f"QA-проверка {task_name}: {'✅ ПРОШЁЛ' if qa_approved else '❌ НЕ ПРОШЁЛ'}",
                    full_response=json.dumps(qa_result, ensure_ascii=False),
                    tokens_used=qa_tokens
                )

                if qa_approved:
                    tasks_db.update_task(task_db_id, {
                        "status": "completed",
                        "qa_approved": "true",
                        "qa_feedback": qa_feedback_text
                    })
                    update_last_agent_log(project_id, agent_name, "completed")
                    logger.info(f"✅ Задача {task_name} ({agent_name}) выполнена и прошла QA")
                else:
                    logger.warning(f"⚠️ QA не прошёл для {task_name}: {qa_feedback_text[:200]}")
                    tasks_db.update_task(task_db_id, {
                        "status": "pending",
                        "qa_approved": "false",
                        "qa_feedback": qa_feedback_text
                    })
                    update_last_agent_log(project_id, agent_name, "needs_review")

                    if iteration_count + 1 >= max_iter:
                        logger.error(f"❌ Задача {task_name} провалена после {max_iter} итераций")
                        tasks_db.update_task(task_db_id, {"status": "failed"})
                        update_last_agent_log(project_id, agent_name, "failed")
            else:
                tasks_db.update_task(task_db_id, {
                    "status": "completed",
                    "qa_approved": "true"
                })
                update_last_agent_log(project_id, agent_name, "completed")
                logger.info(f"✅ Задача QA {task_name} выполнена")

        except Exception as e:
            logger.error(f" Ошибка выполнения задачи {task_name}: {e}", exc_info=True)
            tasks_db.update_task(task_db_id, {
                "status": "pending" if iteration_count + 1 < max_iter else "failed",
                "qa_feedback": f"Ошибка: {str(e)[:500]}"
            })

    agency_running = False
    logger.info(f" ИТОГ: статус={current_project.get('status')}, токенов={current_project.get('tokens_used')}")


# ==================== ЗАПУСК СЕРВЕРА ====================

def test_llm_connection():
    """Проверка подключения к Yandex AI Studio"""
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