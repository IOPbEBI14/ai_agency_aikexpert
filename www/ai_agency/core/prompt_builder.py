"""
Генератор промптов с JSON Schema для строгой валидации ответов LLM.
"""
import json
from typing import Type, Any
from pydantic import BaseModel
from .schemas import (
    PMDecision, PMTaskGraph, PMDecomposition, PMFinalReport, PMHumanReview, PMDeadlockResolution,
    AnalystResponse, ArchitectResponse, DeveloperResponse, QAResponse, TechWriterResponse,
    LeadHunterResponse, SalesResponse, CRMCustomizerResponse
)


def get_json_schema(model_class: Type[BaseModel]) -> str:
    """Получает JSON Schema модели в формате строки."""
    schema = model_class.model_json_schema()
    return json.dumps(schema, indent=2, ensure_ascii=False)


def build_prompt_with_schema(base_prompt: str, model_class: Type[BaseModel]) -> str:
    """
    Добавляет JSON Schema к базовому промпту.
    
    Args:
        base_prompt: Базовый текст промпта из файла
        model_class: Pydantic-модель для валидации
    
    Returns:
        Промпт с JSON Schema
    """
    schema = get_json_schema(model_class)
    
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


# Маппинг агентов к их моделям
AGENT_MODELS = {
    "pm_decision": PMDecision,
    "pm_task_graph": PMTaskGraph,
    "pm_decomposition": PMDecomposition,
    "pm_final_report": PMFinalReport,
    "pm_human_review": PMHumanReview,
    "pm_deadlock": PMDeadlockResolution,
    "analyst": AnalystResponse,
    "architect": ArchitectResponse,
    "developer": DeveloperResponse,
    "qa": QAResponse,
    "tech_writer": TechWriterResponse,
    "lead_hunter": LeadHunterResponse,
    "sales": SalesResponse,
    "crm_customizer": CRMCustomizerResponse,
}


def get_agent_model(agent_name: str) -> Type[BaseModel]:
    """Получает Pydantic-модель для агента."""
    return AGENT_MODELS.get(agent_name)