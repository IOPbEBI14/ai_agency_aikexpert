"""
Парсер ответов LLM с Pydantic-валидацией.

Каноническая реализация находится в schemas.call_and_parse_llm.
Этот модуль является тонкой обёрткой для обратной совместимости.
"""
import json
import logging
from typing import Any, Tuple, Type, TypeVar

from pydantic import BaseModel

from .schemas import call_and_parse_llm, extract_json_from_text

logger = logging.getLogger("LLMParser")

T = TypeVar("T", bound=BaseModel)


# Основная функция — прямой псевдоним канонической реализации
parse_llm_response = call_and_parse_llm


def safe_parse_llm_response(
    call_llm_func,
    agent_name: str,
    system_prompt: str,
    user_task: str,
    response_model: Type[T],
    max_retries: int = 2,
) -> Tuple[Any, int]:
    """
    Безопасная версия parse_llm_response.
    При провале валидации возвращает сырой dict вместо исключения.

    Returns:
        (result, tokens_used) — result может быть Pydantic-моделью или dict
    """
    try:
        return call_and_parse_llm(
            call_llm_func, agent_name, system_prompt, user_task,
            response_model, max_retries,
        )
    except ValueError as e:
        logger.error(f"❌ Критическая ошибка парсинга для {agent_name}: {e}")
        try:
            raw_response, tokens = call_llm_func(agent_name, system_prompt, user_task)
            json_str = extract_json_from_text(raw_response)
            data_dict = json.loads(json_str)
            logger.warning(f"⚠️ Возвращаем сырой dict для {agent_name}")
            return data_dict, tokens
        except Exception:
            logger.error(f"❌ Не удалось получить даже сырой JSON от {agent_name}")
            return None, 0
