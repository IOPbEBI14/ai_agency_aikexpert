"""
Pydantic-модели для строгой валидации ответов LLM.
Каждый агент должен возвращать JSON, соответствующий своей модели.
"""

from pydantic import BaseModel, Field, field_validator, model_validator, ValidationError, ConfigDict
from typing import List, Dict, Any, Optional, Literal, Type, TypeVar
from datetime import datetime
import json
import logging
import re

T = TypeVar('T', bound=BaseModel)

logger = logging.getLogger(__name__)


# ==================== БАЗОВЫЕ МОДЕЛИ ====================

class BaseAgentResponse(BaseModel):
    """Базовая модель для всех ответов агентов."""
    model_config = ConfigDict(extra="allow")  # Разрешаем дополнительные поля для гибкости

# ==================== PM MODELS ====================

class PMDecision(BaseModel):
    """Решение PM о следующем шаге."""
    
    project_status: Literal["in_progress", "completed", "needs_human_review"] = Field(
        description="Статус проекта"
    )
    current_phase: Literal[
        "lead_gen", "sales", "analysis", "architecture", 
        "development", "qa", "documentation"
    ] = Field(description="Текущая фаза проекта")
    next_agent: Optional[str] = Field(
        default=None,
        description="Имя следующего агента или null"
    )
    task_for_next_agent: Optional[str] = Field(
        default=None,
        description="Задача для следующего агента"
    )
    pm_comment: str = Field(description="Комментарий PM")
    
    @field_validator("next_agent")
    @classmethod
    def validate_next_agent(cls, v):
        if v is not None and v not in [
            "lead_hunter", "sales", "analyst", "architect", 
            "developer", "crm_customizer", "qa", "tech_writer", None
        ]:
            raise ValueError(f"Неизвестный агент: {v}")
        return v


class PMTaskGraph(BaseModel):
    """Task Graph, созданный PM."""
    tasks: List[Dict[str, Any]] = Field(description="Список задач")

    # ⭐ НОВОЕ: Нормализация id → task_id
    @model_validator(mode="before")
    @classmethod
    def normalize_task_ids(cls, data):
        """Преобразует 'id' в 'task_id' если 'task_id' отсутствует."""
        if isinstance(data, dict) and "tasks" in data:
            for task in data["tasks"]:
                if isinstance(task, dict):
                    # Если есть 'id', но нет 'task_id' — копируем
                    if "id" in task and "task_id" not in task:
                        task["task_id"] = task["id"]
        return data

    @model_validator(mode="after")
    def validate_task_graph(self):
        """Проверка на циклические зависимости."""
        task_ids = {t.get("task_id") for t in self.tasks}
        
        for task in self.tasks:
            task_id = task.get("task_id")
            depends_on = task.get("depends_on", [])
            
            # Проверка, что все зависимости существуют
            for dep_id in depends_on:
                if dep_id not in task_ids:
                    raise ValueError(
                        f"Задача {task_id} зависит от несуществующей задачи {dep_id}"
                    )
            
            # Проверка на циклы (простая: задача не может зависеть от себя)
            if task_id in depends_on:
                raise ValueError(f"Задача {task_id} зависит от самой себя")
        
        return self
        
class PMDecomposition(BaseModel):
    """Декомпозиция архитектуры на подзадачи."""
    
    subtasks: List[Dict[str, Any]] = Field(description="Список подзадач")
    pm_comment: str = Field(description="Комментарий PM")
    
    @field_validator("subtasks")
    @classmethod
    def validate_subtasks(cls, v):
        if len(v) == 0:
            raise ValueError("Список подзадач не может быть пустым")
        if len(v) > 10:
            raise ValueError("Слишком много подзадач (максимум 10)")
        
        for subtask in v:
            if "subtask_id" not in subtask:
                raise ValueError("Каждая подзадача должна иметь subtask_id")
            if "description" not in subtask:
                raise ValueError("Каждая подзадача должна иметь description")
        
        return v


class PMFinalReport(BaseModel):
    """Финальный отчёт PM."""
    
    project_status: Literal["completed"] = Field(description="Статус проекта")
    final_report: str = Field(description="Полный текст отчёта в markdown")
    metrics: Dict[str, Any] = Field(description="Метрики проекта")
    pm_comment: str = Field(description="Комментарий PM")


class PMHumanReview(BaseModel):
    """Ответ PM на human review."""
    
    updated_task_description: str = Field(description="Обновлённое описание задачи")
    pm_comment: str = Field(description="Комментарий PM")


class PMDeadlockResolution(BaseModel):
    """Решение PM для разрешения тупика."""
    
    analysis: str = Field(description="Анализ ситуации")
    solution: Literal[
        "update_dependencies", "skip_tasks", "create_tasks", "stop_project"
    ] = Field(description="Решение")
    actions: List[Dict[str, Any]] = Field(description="Список действий")
    comment: str = Field(description="Комментарий PM")


# ==================== ANALYST MODELS ====================

class PainPoint(BaseModel):
    """Болевая точка клиента."""
    
    process: str = Field(description="Описание процесса")
    time_per_day_hours: float = Field(description="Время в часах в день")
    cost_per_month_rub: float = Field(description="Стоимость в рублях в месяц")


class ProposedAutomation(BaseModel):
    """Предлагаемая автоматизация."""
    
    solution: str = Field(description="Описание решения")
    tools: List[str] = Field(description="Список инструментов")
    time_saved_hours_per_day: float = Field(description="Экономия времени в часах в день")
    implementation_complexity: Literal["low", "medium", "high"] = Field(
        description="Сложность реализации"
    )


class ROICalculation(BaseModel):
    """Расчёт ROI."""
    
    total_time_saved_hours_per_month: float = Field(description="Общая экономия времени в часах в месяц")
    cost_saved_per_month_rub: float = Field(description="Экономия в рублях в месяц")
    implementation_cost_rub: float = Field(description="Стоимость внедрения в рублях")
    payback_period_months: float = Field(description="Срок окупаемости в месяцах")


class AnalystResponse(BaseModel):
    """Ответ аналитика."""
    
    client_name: str = Field(description="Имя клиента")
    current_pain_points: List[PainPoint] = Field(description="Текущие болевые точки")
    proposed_automation: List[ProposedAutomation] = Field(description="Предлагаемая автоматизация")
    roi_calculation: ROICalculation = Field(description="Расчёт ROI")
    proposal_structure: List[str] = Field(description="Структура коммерческого предложения")
    notes: Optional[str] = Field(default=None, description="Дополнительные заметки")


# ==================== ARCHITECT MODELS ====================

class SystemInfo(BaseModel):
    """Информация о системе."""
    
    name: str = Field(description="Название системы")
    role: Literal["source", "processor", "storage", "destination"] = Field(description="Роль системы")
    api_available: bool = Field(description="Доступен ли API")
    limitations: Optional[str] = Field(default=None, description="Ограничения")


class DataFlowStep(BaseModel):
    """Шаг потока данных."""
    step: int = Field(description="Номер шага")
    from_system: str = Field(alias="from", description="Система-источник")
    to_system: str = Field(alias="to", description="Система-приёмник")
    trigger: str = Field(description="Триггер")
    data: str = Field(description="Передаваемые данные")
    transformation: str = Field(description="Трансформация данных")

    model_config = ConfigDict(populate_by_name=True)

class ArchitectResponse(BaseModel):
    """Ответ архитектора."""
    
    summary: str = Field(description="Краткое описание архитектуры")
    approach: str = Field(description="Общий подход")
    systems: List[SystemInfo] = Field(description="Список систем")
    data_flow: List[DataFlowStep] = Field(description="Поток данных")
    tech_stack: List[str] = Field(description="Технологический стек")
    estimated_complexity: Literal["low", "medium", "high"] = Field(description="Оценочная сложность")
    estimated_time_hours: int = Field(description="Оценочное время в часах")
    risks: List[str] = Field(description="Список рисков")
    recommendations: str = Field(description="Рекомендации")


# ==================== DEVELOPER MODELS ====================

class DeveloperFile(BaseModel):
    """Файл, созданный разработчиком."""
    
    name: str = Field(description="Имя файла")
    type: Literal["n8n_workflow", "javascript", "config"] = Field(description="Тип файла")
    description: str = Field(description="Описание файла")


class DeveloperResponse(BaseModel):
    """Ответ разработчика."""
    
    summary: str = Field(description="Что было разработано")
    workflow_name: Optional[str] = Field(default=None, description="Название workflow")
    n8n_json: Optional[Dict[str, Any]] = Field(default=None, description="JSON схемы n8n")
    files: List[DeveloperFile] = Field(description="Список файлов")
    setup_instructions: List[str] = Field(description="Инструкции по настройке")
    testing_steps: List[str] = Field(description="Шаги тестирования")
    notes: Optional[str] = Field(default=None, description="Дополнительные заметки")


# ==================== QA MODELS ====================

class QAIssue(BaseModel):
    """Проблема, найденная QA."""
    
    severity: Literal["critical", "high", "medium", "low"] = Field(description="Серьёзность")
    type: Literal["validation", "logic", "security", "performance"] = Field(description="Тип проблемы")
    description: str = Field(description="Описание проблемы")
    location: str = Field(description="Где найдено")
    recommendation: str = Field(description="Рекомендация по исправлению")


class QATestCase(BaseModel):
    """Тест-кейс."""
    
    name: str = Field(description="Название теста")
    status: Literal["passed", "failed"] = Field(description="Статус теста")
    description: str = Field(description="Что проверялось")


class QAResponse(BaseModel):
    """Ответ QA."""
    
    summary: str = Field(description="Общий результат проверки")
    tests_total: int = Field(description="Общее количество тестов")
    tests_passed: int = Field(description="Количество пройденных тестов")
    tests_failed: int = Field(description="Количество проваленных тестов")
    issues: List[QAIssue] = Field(description="Список проблем")
    warnings: List[str] = Field(description="Список предупреждений")
    recommendations: List[str] = Field(description="Список рекомендаций")
    test_cases: List[QATestCase] = Field(description="Список тест-кейсов")


# ==================== TECH WRITER MODELS ====================

class DocumentSection(BaseModel):
    """Секция документа."""
    
    title: str = Field(description="Название раздела")
    content: str = Field(description="Содержание раздела")
    screenshot_needed: bool = Field(description="Нужен ли скриншот")
    screenshot_description: Optional[str] = Field(default=None, description="Описание скриншота")


class Document(BaseModel):
    """Документ."""
    
    title: str = Field(description="Название документа")
    type: Literal["user_guide", "tech_guide", "video_script", "faq", "checklist"] = Field(
        description="Тип документа"
    )
    audience: str = Field(description="Для кого документ")
    sections: List[DocumentSection] = Field(description="Список секций")


class VideoScript(BaseModel):
    """Скрипт видео."""
    
    title: str = Field(description="Название видео")
    duration_minutes: int = Field(description="Длительность в минутах")
    script: str = Field(description="Текст для озвучки")
    visual_cues: List[str] = Field(description="Что показывать на экране")


class FAQItem(BaseModel):
    """Элемент FAQ."""
    
    question: str = Field(description="Вопрос")
    answer: str = Field(description="Ответ")


class TechWriterResponse(BaseModel):
    """Ответ технического писателя."""
    
    summary: str = Field(description="Что было создано")
    documents: List[Document] = Field(description="Список документов")
    video_scripts: List[VideoScript] = Field(description="Список видео-скриптов")
    faq: List[FAQItem] = Field(description="Список FAQ")
    checklist: List[str] = Field(description="Чек-лист")
    notes: Optional[str] = Field(default=None, description="Дополнительные рекомендации")


# ==================== LEAD HUNTER MODELS ====================

class Lead(BaseModel):
    """Лид."""
    
    company_name: str = Field(description="Название компании")
    marketplace: str = Field(description="Маркетплейс")
    category: str = Field(description="Категория товаров")
    estimated_revenue: Optional[str] = Field(default=None, description="Примерный оборот")
    pain_points: List[str] = Field(description="Болевые точки")
    contact_telegram: Optional[str] = Field(default=None, description="Telegram контакт")
    contact_email: Optional[str] = Field(default=None, description="Email контакт")
    contact_phone: Optional[str] = Field(default=None, description="Телефон контакт")
    source: str = Field(description="Источник")


class LeadHunterResponse(BaseModel):
    """Ответ Lead Hunter."""
    
    leads_found: List[Lead] = Field(description="Список найденных лидов")
    total_found: int = Field(description="Общее количество найденных лидов")
    notes: Optional[str] = Field(default=None, description="Комментарий о качестве лидов")


# ==================== SALES MODELS ====================

class SalesMessage(BaseModel):
    """Сообщение для лида."""
    
    lead_name: str = Field(description="Название компании")
    message_text: str = Field(description="Текст сообщения")
    channel: Literal["telegram", "email", "phone"] = Field(description="Канал связи")
    personalization_points: List[str] = Field(description="Что упомянули о их бизнесе")


class SalesResponse(BaseModel):
    """Ответ Sales."""
    
    messages: List[SalesMessage] = Field(description="Список сообщений")
    qualification_questions: List[str] = Field(description="Вопросы для квалификации лида")
    next_steps: str = Field(description="Рекомендации по дальнейшим действиям")


# ==================== CRM CUSTOMIZER MODELS ====================

class CustomField(BaseModel):
    """Кастомное поле."""
    
    name: str = Field(description="Название поля")
    type: Literal["text", "number", "date", "select", "url"] = Field(description="Тип поля")
    purpose: str = Field(description="Зачем нужно поле")


class Entity(BaseModel):
    """Сущность CRM."""
    
    name: str = Field(description="Название сущности")
    custom_fields: List[CustomField] = Field(description="Список кастомных полей")


class Pipeline(BaseModel):
    """Воронка продаж."""
    
    name: str = Field(description="Название воронки")
    stages: List[str] = Field(description="Список этапов")


class BusinessProcess(BaseModel):
    """Бизнес-процесс."""
    
    trigger: str = Field(description="Событие-триггер")
    actions: List[str] = Field(description="Список действий")
    purpose: str = Field(description="Зачем нужен процесс")


class FieldMapping(BaseModel):
    """Маппинг полей."""
    
    from_system: str = Field(description="Система-источник")
    from_field: str = Field(description="Поле-источник")
    to_system: str = Field(description="Система-приёмник")
    to_field: str = Field(description="Поле-приёмник")


class CRMCustomizerResponse(BaseModel):
    """Ответ CRM Customizer."""
    
    summary: str = Field(description="Что было настроено")
    platform: Literal["Битрикс24", "AmoCRM", "МойСклад", "Bpium"] = Field(description="Платформа")
    entities: List[Entity] = Field(description="Список сущностей")
    pipelines: List[Pipeline] = Field(description="Список воронок")
    business_processes: List[BusinessProcess] = Field(description="Список бизнес-процессов")
    field_mapping: List[FieldMapping] = Field(description="Маппинг полей")
    setup_steps: List[str] = Field(description="Шаги настройки")
    notes: Optional[str] = Field(default=None, description="Дополнительные рекомендации")


# ==================== УТИЛИТЫ ДЛЯ РАБОТЫ С МОДЕЛЯМИ ====================

def get_model_schema(model_class: type) -> str:
    """
    Получает JSON Schema модели в формате строки.
    Используется для вставки в промпты.
    """
    schema = model_class.model_json_schema()
    return json.dumps(schema, indent=2, ensure_ascii=False)


def get_model_example(model_class: type) -> str:
    """
    Получает пример JSON для модели.
    Используется для вставки в промпты.
    """
    # Создаём пример данных (упрощённый)
    try:
        # Пытаемся создать модель с минимальными данными
        if model_class == PMDecision:
            example = PMDecision(
                project_status="in_progress",
                current_phase="analysis",
                next_agent="analyst",
                task_for_next_agent="Провести анализ потребностей клиента",
                pm_comment="Начинаем с анализа"
            )
        elif model_class == AnalystResponse:
            example = AnalystResponse(
                client_name="ООО Ромашка",
                current_pain_points=[
                    PainPoint(
                        process="Ручной перенос данных",
                        time_per_day_hours=2.0,
                        cost_per_month_rub=20000.0
                    )
                ],
                proposed_automation=[
                    ProposedAutomation(
                        solution="Автоматическая синхронизация",
                        tools=["n8n", "API WB"],
                        time_saved_hours_per_day=1.5,
                        implementation_complexity="medium"
                    )
                ],
                roi_calculation=ROICalculation(
                    total_time_saved_hours_per_month=30.0,
                    cost_saved_per_month_rub=30000.0,
                    implementation_cost_rub=50000.0,
                    payback_period_months=1.7
                ),
                proposal_structure=[
                    "Слайд 1: Проблема",
                    "Слайд 2: Решение"
                ]
            )
        else:
            # Для других моделей возвращаем пустой пример
            return "{}"
        
        return example.model_dump_json(indent=2)
    except Exception as e:
        logger.warning(f"Не удалось создать пример для {model_class.__name__}: {e}")
        return "{}"
        
def extract_json_from_text(text: str) -> str:
    """
    Извлекает JSON из текста ответа LLM.
    Поддерживает как объекты {...}, так и массивы [...].
    """
    text = text.strip()
    
    # Определяем тип JSON
    if text.startswith('{'):
        start = 0
        end_char = '}'
        end = text.rfind('}')
    elif text.startswith('['):
        start = 0
        end_char = ']'
        end = text.rfind(']')
    else:
        # Ищем первый { или [
        start_obj = text.find('{')
        start_arr = text.find('[')
        
        if start_obj == -1 and start_arr == -1:
            raise ValueError("JSON не найден в ответе")
        
        if start_obj == -1:
            start = start_arr
            end_char = ']'
            end = text.rfind(']')
        elif start_arr == -1:
            start = start_obj
            end_char = '}'
            end = text.rfind('}')
        else:
            # Берём тот, который раньше
            if start_obj < start_arr:
                start = start_obj
                end_char = '}'
                end = text.rfind('}')
            else:
                start = start_arr
                end_char = ']'
                end = text.rfind(']')
    
    if start == -1 or end == -1 or end <= start:
        raise ValueError("JSON не найден в ответе")
    
    return text[start:end+1]

def call_and_parse_llm(
    call_llm_func,
    agent_name: str,
    system_prompt: str,
    user_task: str,
    response_model: Type[T],
    max_retries: int = 2
) -> T:
    """
    Вызывает LLM, парсит JSON и валидирует через Pydantic.
    
    Args:
        call_llm_func: Функция call_llm из main.py
        agent_name: Имя агента
        system_prompt: Системный промпт
        user_task: Задача для агента
        response_model: Pydantic-модель для валидации
        max_retries: Максимальное количество попыток
    
    Returns:
        Валидированная Pydantic-модель
    
    Raises:
        ValueError: Если LLM не смог вернуть валидный JSON
    """
    current_prompt = user_task
    
    for attempt in range(max_retries + 1):
        try:
            # Вызываем LLM
            raw_response, tokens = call_llm_func(agent_name, system_prompt, current_prompt)
            
            logger.info(f"📏 Получен ответ от {agent_name}: {len(raw_response)} символов")
            
            # Извлекаем JSON
            json_str = extract_json_from_text(raw_response)
            data_dict = json.loads(json_str)
            
            # Валидируем через Pydantic
            validated_model = response_model(**data_dict)
            
            logger.info(f"✅ Успешная валидация для {response_model.__name__}")
            return validated_model, tokens
            
        except json.JSONDecodeError as e:
            logger.warning(f"⚠️ Попытка {attempt+1}: невалидный JSON: {e}")
            
            if attempt == max_retries:
                raise ValueError(
                    f"LLM не смог вернуть валидный JSON для {response_model.__name__}: {e}"
                )
            
            # Отправляем ошибку в LLM для исправления
            current_prompt = (
                f"{user_task}\n\n"
                f"⚠️ ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ БЫЛ НЕВАЛИДНЫМ.\n"
                f"Ошибка: {e}\n"
                f"Ты должен вернуть ТОЛЬКО валидный JSON, строго соответствующий схеме. "
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
                f"Ошибки:\n{e}\n"
                f"Ты должен исправить эти ошибки и вернуть ТОЛЬКО валидный JSON.\n"
                f"Попробуй снова."
            )
        
        except Exception as e:
            logger.error(f"❌ Неожиданная ошибка при парсинге: {e}")
            
            if attempt == max_retries:
                raise ValueError(f"Не удалось получить валидный ответ от {agent_name}: {e}")
    
    raise ValueError(f"Не удалось получить валидный ответ от {agent_name} после {max_retries+1} попыток")        