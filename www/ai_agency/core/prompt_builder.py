"""
Генератор промптов с JSON Schema для строгой валидации ответов LLM.

Единый источник истины для схем — модули schemas.py (AGENT_MODELS, get_model_schema).
Этот модуль предоставляет только build_prompt_with_schema и get_agent_model.
"""
from typing import Type
from pydantic import BaseModel
from .schemas import AGENT_MODELS, get_model_schema


def build_prompt_with_schema(base_prompt: str, model_class: Type[BaseModel]) -> str:
    """
    Добавляет JSON Schema к базовому промпту.

    Args:
        base_prompt: Базовый текст промпта из файла
        model_class: Pydantic-модель для валидации

    Returns:
        Промпт с JSON Schema
    """
    schema = get_model_schema(model_class)

    schema_section = f"""

═══════════════════════════════════════════════════════════
СТРОГАЯ СТРУКТУРА ОТВЕТА (JSON Schema)
═══════════════════════════════════════════════════════════

Ты ДОЛЖЕН вернуть ТОЛЬКО валидный JSON, строго соответствующий этой схеме:

{schema}

ПРАВИЛА:
1. Верни ТОЛЬКО JSON, без комментариев до или после
2. Все обязательные поля должны быть заполнены
3. Типы данных должны точно соответствовать схеме (string, number, boolean, array)
4. Если поле Optional — можешь не включать его или установить null
5. Убедись, что все скобки и кавычки закрыты

═══════════════════════════════════════════════════════════
"""

    return base_prompt + schema_section


def get_agent_model(agent_name: str) -> Type[BaseModel]:
    """Получает Pydantic-модель для агента."""
    return AGENT_MODELS.get(agent_name)
