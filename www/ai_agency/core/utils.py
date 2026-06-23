"""
Общие утилиты для ИИ-агентства.
Вынесены сюда для избежания циклических импортов.
"""
import os
import json
from datetime import datetime
import logging
import requests
from typing import Dict, Any, Optional
from core.config import Config
from core.nocodb import NocoDBClient

logger = logging.getLogger("Utils")
db = NocoDBClient()


def load_prompt(agent_name: str) -> str:
    """Загружает системный промпт из файла prompts/{agent_name}_prompt.txt"""
    prompt_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
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
    
    for attempt in range(max_retries + 1):
        try:
            logger.info(f"🤖 Вызов агента: {agent_name} (попытка {attempt + 1}/{max_retries + 1})")
            response = requests.post(url, json=payload, headers=headers, timeout=180)
            
            if response.status_code != 200:
                error_msg = f"❌ LLM вернул статус {response.status_code}: {response.text[:300]}"
                logger.error(error_msg)
                
                if attempt == max_retries:
                    raise RuntimeError(error_msg)
                
                continue  # ⭐ ВАЖНО: Переходим к следующей попытке, а не обрабатываем ошибку как успех
                
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
    
    raise RuntimeError(f"Не удалось получить ответ от {agent_name}")


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
Выполнить задачу строго по описанию
Вернуть результат в формате JSON
Убедиться, что JSON полностью закрыт
Верни ТОЛЬКО валидный JSON.
"""
    return task


def log_to_agent_logs(project_id: Optional[int], agent_name: str, status: str,
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
        return qa_result
    except Exception as e:
        logger.error(f"❌ Ошибка QA: {e}")
        return {"approved": True, "feedback": "QA ошибка, пропускаем", "tokens_used": 0}