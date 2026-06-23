"""
Универсальный парсер ответов LLM с Pydantic-валидацией.
Заменяет хрупкие функции типа build_summary().
"""
import json
import logging
from typing import Type, Any, Tuple, TypeVar
from pydantic import BaseModel, ValidationError
from .schemas import extract_json_from_text

logger = logging.getLogger("LLMParser")

T = TypeVar('T', bound=BaseModel)


def parse_llm_response(
    call_llm_func,
    agent_name: str,
    system_prompt: str,
    user_task: str,
    response_model: Type[T],
    max_retries: int = 2
) -> Tuple[T, int]:
    """
    Вызывает LLM, парсит JSON и валидирует через Pydantic.
    
    Args:
        call_llm_func: Функция call_llm из main.py
        agent_name: Имя агента
        system_prompt: Системный промпт (уже с JSON Schema)
        user_task: Задача для агента
        response_model: Pydantic-модель для валидации
        max_retries: Максимальное количество попыток
    
    Returns:
        (validated_model, tokens_used)
    
    Raises:
        ValueError: Если LLM не смог вернуть валидный JSON
    """
    current_prompt = user_task
    
    for attempt in range(max_retries + 1):
        try:
            # Вызываем LLM
            raw_response, tokens = call_llm_func(agent_name, system_prompt, current_prompt)
            
            logger.info(f"📏 Получен ответ от {agent_name}: {len(raw_response)} символов")
            
            # Извлекаем JSON из текста
            json_str = extract_json_from_text(raw_response)
            data_dict = json.loads(json_str)
            
            # Валидируем через Pydantic
            validated_model = response_model(**data_dict)
            
            logger.info(f"✅ Успешная валидация для {response_model.__name__}")
            return validated_model, tokens
            
        except json.JSONDecodeError as e:
            logger.warning(f"️ Попытка {attempt+1}: невалидный JSON: {e}")
            
            if attempt == max_retries:
                raise ValueError(
                    f"LLM не смог вернуть валидный JSON для {response_model.__name__}: {e}"
                )
            
            # Отправляем ошибку в LLM для исправления
            current_prompt = (
                f"{user_task}\n\n"
                f"⚠️ ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ БЫЛ НЕВАЛИДНЫМ.\n"
                f"Ошибка парсинга JSON: {e}\n"
                f"Ты должен вернуть ТОЛЬКО валидный JSON, строго соответствующий схеме.\n"
                f"Убедись, что все скобки закрыты, все кавычки экранированы.\n"
                f"Попробуй снова."
            )
            
        except ValidationError as e:
            logger.warning(f"⚠️ Попытка {attempt+1}: ошибка валидации Pydantic: {e}")
            
            if attempt == max_retries:
                raise ValueError(
                    f"LLM вернул JSON, но он не соответствует схеме {response_model.__name__}: {e}"
                )
            
            # Отправляем ошибку валидации в LLM
            current_prompt = (
                f"{user_task}\n\n"
                f"⚠️ ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ ПРОШЁЛ ПАРСИНГ JSON, НО НЕ ПРОШЁЛ ВАЛИДАЦИЮ.\n"
                f"Ошибки валидации:\n{e}\n"
                f"Ты должен исправить эти ошибки и вернуть ТОЛЬКО валидный JSON.\n"
                f"Попробуй снова."
            )
        
        except Exception as e:
            logger.error(f"❌ Неожиданная ошибка при парсинге: {e}")
            
            if attempt == max_retries:
                raise ValueError(f"Не удалось получить валидный ответ от {agent_name}: {e}")
    
    raise ValueError(f"Не удалось получить валидный ответ от {agent_name} после {max_retries+1} попыток")


def safe_parse_llm_response(
    call_llm_func,
    agent_name: str,
    system_prompt: str,
    user_task: str,
    response_model: Type[T],
    max_retries: int = 2
) -> Tuple[Any, int]:
    """
    Безопасная версия parse_llm_response.
    Если валидация не прошла — возвращает сырой JSON dict.
    
    Returns:
        (result, tokens_used) — result может быть Pydantic-моделью или dict
    """
    try:
        return parse_llm_response(
            call_llm_func, agent_name, system_prompt, user_task,
            response_model, max_retries
        )
    except ValueError as e:
        logger.error(f"❌ Критическая ошибка парсинга для {agent_name}: {e}")
        
        # Пытаемся хотя бы получить сырой JSON
        try:
            raw_response, tokens = call_llm_func(agent_name, system_prompt, user_task)
            json_str = extract_json_from_text(raw_response)
            data_dict = json.loads(json_str)
            logger.warning(f"⚠️ Возвращаем сырой dict для {agent_name}")
            return data_dict, tokens
        except:
            logger.error(f"❌ Не удалось получить даже сырой JSON от {agent_name}")
            return None, 0