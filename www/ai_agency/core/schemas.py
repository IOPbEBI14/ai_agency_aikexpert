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
    """Базовая модель с разрешёнными дополнительными полями.

    Намеренно не используется как родительский класс для агентских схем:
    - Pydantic v2 по умолчанию применяет extra="ignore" (лишние поля отбрасываются тихо)
    - Это безопасное поведение для валидации LLM-ответов
    - LLM часто добавляет handoff_to_* поля, которые оркестратор игнорирует —
      это нормально, они используются только как подсказки в промптах

    Оставлен для случаев, когда нужно явно сохранять extra-поля:
    например, если агент возвращает незаданные заранее метаданные.
    """
    model_config = ConfigDict(extra="allow")

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
            "client_hunter", "lead_hunter", "sales", "analyst", "architect",
            "developer", "crm_customizer", "qa", "tech_writer", None
        ]:
            raise ValueError(f"Неизвестный агент: {v}")
        return v


class PMTaskGraph(BaseModel):
    """Task Graph, созданный PM."""
    tasks: List[Dict[str, Any]] = Field(description="Список задач")
    excluded_agents: List[str] = Field(
        default=[],
        description="Список агентов, которые не нужны для этого проекта"
    )
    reasoning: str = Field(
        default="",
        description="Объяснение, почему некоторые агенты исключены"
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_task_ids(cls, data):
        """Преобразует 'id' в 'task_id' если 'task_id' отсутствует."""
        if isinstance(data, dict) and "tasks" in data:
            for task in data["tasks"]:
                if isinstance(task, dict):
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
            
            for dep_id in depends_on:
                if dep_id not in task_ids:
                    raise ValueError(
                        f"Задача {task_id} зависит от несуществующей задачи {dep_id}"
                    )
            
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
            mode = (subtask.get("artifact_mode") or "").strip().lower()
            if mode and mode not in ("full_workflow", "spec", "prep"):
                raise ValueError(
                    "artifact_mode должен быть full_workflow | spec | prep "
                    f"(получено: {mode})"
                )

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
        "update_dependencies", "skip_tasks", "create_tasks",
        "stop_project", "need_human_review",
    ] = Field(description="Решение")
    actions: List[Dict[str, Any]] = Field(description="Список действий")
    comment: str = Field(description="Комментарий PM")


# ==================== ANALYST MODELS ====================

class PainPoint(BaseModel):
    """Болевая точка клиента."""
    process: str = Field(default="Не указано", description="Описание процесса")
    time_per_day_hours: float = Field(default=0.0, description="Время в часах в день")
    cost_per_month_rub: float = Field(default=0.0, description="Стоимость в рублях в месяц")


class ProposedAutomation(BaseModel):
    """Предлагаемая автоматизация."""
    solution: str = Field(default="Не указано", description="Описание решения")
    tools: List[str] = Field(default_factory=list, description="Список инструментов")
    time_saved_hours_per_day: float = Field(default=0.0, description="Экономия времени в часах в день")
    implementation_complexity: Literal["low", "medium", "high"] = Field(
        default="medium", description="Сложность реализации"
    )


class ROICalculation(BaseModel):
    """Расчёт ROI."""
    total_time_saved_hours_per_month: float = Field(default=0.0, description="Общая экономия времени в часах в месяц")
    cost_saved_per_month_rub: float = Field(default=0.0, description="Экономия в рублях в месяц")
    implementation_cost_rub: float = Field(default=0.0, description="Стоимость внедрения в рублях")
    payback_period_months: float = Field(default=0.0, description="Срок окупаемости в месяцах")


class AnalystResponse(BaseModel):
    """Ответ аналитика."""
    client_name: str = Field(default="Неизвестный клиент", description="Имя клиента")
    current_pain_points: List[PainPoint] = Field(default_factory=list, description="Текущие болевые точки")
    proposed_automation: List[ProposedAutomation] = Field(default_factory=list, description="Предлагаемая автоматизация")
    roi_calculation: ROICalculation = Field(default_factory=ROICalculation, description="Расчёт ROI")
    proposal_structure: List[str] = Field(default_factory=list, description="Структура коммерческого предложения")
    notes: Optional[str] = Field(default=None, description="Дополнительные заметки")
    handoff_to_architect: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Структурированный хэндофф для архитектора: required_integrations, "
            "data_volume_estimate, priority_automations, constraints. "
            "Сохраняется в analyst_context и передаётся architect через input_data."
        ),
    )
    
    @model_validator(mode="before")
    @classmethod
    def fill_missing_fields(cls, data):
        """Заполняет отсутствующие поля дефолтными значениями."""
        if not isinstance(data, dict):
            return data
        
        # Заполняем client_name
        if not data.get("client_name"):
            data["client_name"] = "Потенциальный клиент"
        
        # Заполняем pain_points если пусто
        if not data.get("current_pain_points"):
            data["current_pain_points"] = [
                {
                    "process": "Ручная обработка заказов",
                    "time_per_day_hours": 2.0,
                    "cost_per_month_rub": 20000.0
                }
            ]
        
        # Заполняем proposed_automation если пусто
        if not data.get("proposed_automation"):
            data["proposed_automation"] = [
                {
                    "solution": "Автоматизация обработки заказов",
                    "tools": ["n8n"],
                    "time_saved_hours_per_day": 1.5,
                    "implementation_complexity": "medium"
                }
            ]
        
        # Заполняем roi_calculation если пусто или содержит null
        roi = data.get("roi_calculation", {})
        if not roi or isinstance(roi, dict):
            data["roi_calculation"] = {
                "total_time_saved_hours_per_month": roi.get("total_time_saved_hours_per_month") or 30.0,
                "cost_saved_per_month_rub": roi.get("cost_saved_per_month_rub") or 30000.0,
                "implementation_cost_rub": roi.get("implementation_cost_rub") or 50000.0,
                "payback_period_months": roi.get("payback_period_months") or 1.7
            }
        
        # Заполняем proposal_structure если пусто
        if not data.get("proposal_structure"):
            data["proposal_structure"] = [
                "Слайд 1: Проблема клиента",
                "Слайд 2: Наше решение",
                "Слайд 3: Экономика (ROI)"
            ]
        
        return data
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
    handoff_to_developer: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Node-level blueprint для developer. Содержит: "
            "workflow_blueprint (nodes[], connections[], field_mapping[], error_handling[]), "
            "api_endpoints[], credentials_needed[], implementation_order[]. "
            "Разработчик сериализует этот blueprint в валидный n8n JSON."
        ),
    )


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
    type: Literal[
        "user_guide", "tech_guide", "video_script", "faq", "checklist", "commercial_proposal"
    ] = Field(description="Тип документа")
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


# ==================== CLIENT HUNTER (МОНЕТИЗАЦИЯ) ====================

class ClientUSP(BaseModel):
    """Уникальное торговое предложение для найденного клиента."""

    headline: str = Field(description="Короткий заголовок УТП (1 предложение)")
    value_proposition: str = Field(
        description="Уникальное торговое предложение: чем агентство полезно именно этому клиенту"
    )
    differentiators: List[str] = Field(
        description="2–5 пунктов отличия от типовых офферов конкурентов"
    )
    call_to_action: str = Field(description="Призыв к действию / следующий шаг")


class ClientProspect(BaseModel):
    """Потенциальный клиент из открытых источников (Google)."""

    company_name: str = Field(description="Название компании / бренда")
    website: Optional[str] = Field(default=None, description="URL из Google")
    snippet: Optional[str] = Field(default=None, description="Сниппет Google")
    niche: str = Field(description="Ниша / отрасль")
    decision_maker_role: Optional[str] = Field(
        default=None,
        description=(
            "Гипотеза роли ЛПР (главврач / собственник / маркетолог / коммерческий директор). "
            "Только если следует из сниппета/сайта — не выдумывать ФИО."
        ),
    )
    contact_email: Optional[str] = Field(
        default=None, description="Email с сайта/сниппета (не выдумывать)"
    )
    contact_phone: Optional[str] = Field(
        default=None, description="Телефон с сайта/сниппета (не выдумывать)"
    )
    contact_telegram: Optional[str] = Field(
        default=None, description="Telegram с сайта/сниппета (не выдумывать)"
    )
    contacts_note: Optional[str] = Field(
        default=None,
        description="Откуда контакты: snippet / website_scrape / not_found",
    )
    pain_hypothesis: List[str] = Field(
        description="Гипотезы болей на основе открытых данных"
    )
    usp: ClientUSP = Field(description="Персональное УТП для этого клиента")
    source: Literal["google"] = Field(
        default="google", description="Источник — только Google (открытый поиск)"
    )
    source_query: Optional[str] = Field(
        default=None, description="Поисковый запрос Google, по которому найден клиент"
    )


class ClientHunterResponse(BaseModel):
    """Ответ агента монетизации: поиск клиентов через Google + УТП."""

    summary: str = Field(description="Краткий итог поиска и офферов")
    search_queries: List[str] = Field(
        description="Запросы, которые нужно/были выполнены в Google"
    )
    clients: List[ClientProspect] = Field(
        description="Найденные клиенты с персональным УТП"
    )
    total_found: int = Field(description="Количество клиентов с УТП")
    notes: Optional[str] = Field(
        default=None,
        description="Ограничения поиска, качество источников, рекомендации",
    )
    handoff_to_sales: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Хэндофф для sales: recommended_approach, priority_clients[], "
            "usp_highlights[]. Только на основе открытых Google-данных."
        ),
    )


# ==================== LEAD HUNTER MODELS ====================

class Lead(BaseModel):
    """Лид из открытого Google/OpenSERP-поиска (не выдуманный)."""

    company_name: str = Field(description="Название компании — из title SERP")
    marketplace: str = Field(
        description="Маркетплейс / канал (WB, Ozon, open_web) — гипотеза по сниппету"
    )
    category: str = Field(description="Категория товаров / ниша")
    estimated_revenue: Optional[str] = Field(
        default=None,
        description="Оборот — только если явно следует из открытых данных, иначе null",
    )
    pain_points: List[str] = Field(description="Гипотезы болей по сниппету и goal")
    contact_telegram: Optional[str] = Field(
        default=None, description="Telegram — только из сниппета/сайта, иначе null"
    )
    contact_email: Optional[str] = Field(
        default=None, description="Email — только из сниппета/сайта, иначе null"
    )
    contact_phone: Optional[str] = Field(
        default=None, description="Телефон — только из сниппета/сайта, иначе null"
    )
    website: Optional[str] = Field(
        default=None, description="URL из google_search_results.link"
    )
    source_url: Optional[str] = Field(
        default=None, description="Тот же URL результата поиска (обязателен для валидного лида)"
    )
    source_query: Optional[str] = Field(
        default=None, description="Запрос OpenSERP, по которому найден результат"
    )
    source: str = Field(
        description="Источник: openserp / google + краткое описание, без выдуманного парсинга"
    )


class LeadHunterResponse(BaseModel):
    """Ответ Lead Hunter — только по google_search_results (OpenSERP)."""

    leads_found: List[Lead] = Field(description="Список найденных лидов")
    total_found: int = Field(description="Общее количество найденных лидов")
    search_queries: List[str] = Field(
        default_factory=list,
        description="Запросы OpenSERP / рекомендованные prospect-запросы",
    )
    notes: Optional[str] = Field(default=None, description="Комментарий о качестве лидов")
    handoff_to_sales: Optional[Dict[str, Any]] = Field(
        default=None,
        description=(
            "Структурированный хэндофф для Sales: recommended_approach, "
            "key_pain_points, best_contacts. "
            "Сохраняется в leads_context и передаётся sales через input_data."
        ),
    )


# ==================== SALES MODELS ====================

class SalesMessage(BaseModel):
    """Сообщение для лида (текст для ручной отправки / выгрузки)."""

    lead_name: str = Field(description="Название компании")
    message_text: str = Field(description="Текст сообщения")
    channel: Literal["telegram", "email", "phone"] = Field(description="Канал связи")
    personalization_points: List[str] = Field(description="Что упомянули о их бизнесе")
    subject: Optional[str] = Field(
        default=None, description="Тема письма (для channel=email)"
    )
    to_email: Optional[str] = Field(
        default=None, description="Email получателя из карточки клиента (не выдумывать)"
    )
    to_phone: Optional[str] = Field(default=None, description="Телефон из карточки")
    to_telegram: Optional[str] = Field(default=None, description="Telegram из карточки")
    website: Optional[str] = Field(default=None, description="Сайт компании")
    decision_maker_role: Optional[str] = Field(
        default=None, description="Роль ЛПР (гипотеза из карточки)"
    )


class SalesResponse(BaseModel):
    """Ответ Sales: готовые тексты (отправка пока ручная через выгрузку)."""

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


# ==================== МАППИНГ АГЕНТОВ → PYDANTIC-МОДЕЛИ ====================

AGENT_MODELS: dict = {
    # PM-варианты
    "pm_decision": PMDecision,
    "pm_task_graph": PMTaskGraph,
    "pm_decomposition": PMDecomposition,
    "pm_final_report": PMFinalReport,
    "pm_human_review": PMHumanReview,
    "pm_deadlock": PMDeadlockResolution,
    # Рабочие агенты
    "analyst": AnalystResponse,
    "architect": ArchitectResponse,
    "developer": DeveloperResponse,
    "qa": QAResponse,
    "tech_writer": TechWriterResponse,
    "client_hunter": ClientHunterResponse,
    "lead_hunter": LeadHunterResponse,
    "sales": SalesResponse,
    "crm_customizer": CRMCustomizerResponse,
}

# ==================== УТИЛИТЫ ДЛЯ РАБОТЫ С МОДЕЛЯМИ ====================

def get_model_schema(model_class: type) -> str:
    """
    Получает JSON Schema модели в формате строки.
    Используется для вставки в промпты.
    """
    schema = model_class.model_json_schema()
    return json.dumps(schema, indent=2, ensure_ascii=False)


_MODEL_EXAMPLES: dict = {}


def _build_examples() -> dict:
    """Строит словарь примеров для всех моделей (вызывается один раз)."""
    return {
        PMDecision: {
            "project_status": "in_progress",
            "current_phase": "analysis",
            "next_agent": "analyst",
            "task_for_next_agent": "Провести анализ потребностей клиента",
            "pm_comment": "Начинаем с анализа потребностей",
        },
        PMTaskGraph: {
            "tasks": [
                {
                    "task_id": "task_001",
                    "agent_name": "analyst",
                    "task_description": "Анализ потребностей клиента",
                    "depends_on": [],
                    "input_data": {},
                    "max_iterations": 3,
                }
            ],
            "excluded_agents": ["lead_hunter", "sales"],
            "reasoning": "Клиент уже квалифицирован, начинаем с анализа",
        },
        PMDecomposition: {
            "subtasks": [
                {
                    "subtask_id": "dev_001",
                    "description": "Создать webhook для Telegram в n8n",
                    "depends_on": [],
                    "context": "Из архитектуры: Telegram Bot API, endpoint /telegram",
                }
            ],
            "pm_comment": "Разбил архитектуру на 1 подзадачу для разработчика",
        },
        PMFinalReport: {
            "project_status": "completed",
            "final_report": "# Отчёт\n\n## Что сделано\nАвтоматизирована обработка заявок.",
            "metrics": {"tasks_completed": 5, "total_tokens_used": 50000},
            "pm_comment": "Проект завершён успешно",
        },
        PMHumanReview: {
            "updated_task_description": "Уточнённая задача после ревью",
            "pm_comment": "Обновлено по комментариям ревьюера",
        },
        PMDeadlockResolution: {
            "analysis": "Задача застряла из-за неверных зависимостей",
            "solution": "update_dependencies",
            "actions": [
                {
                    "action": "update_task",
                    "task_id": "task_002",
                    "new_depends_on": ["task_001"],
                }
            ],
            "comment": "Исправляем зависимости для разблокировки",
        },
        AnalystResponse: {
            "client_name": "ООО Ромашка",
            "current_pain_points": [
                {
                    "process": "Ручной перенос данных из WB",
                    "time_per_day_hours": 2.0,
                    "cost_per_month_rub": 20000.0,
                }
            ],
            "proposed_automation": [
                {
                    "solution": "Автоматическая синхронизация через n8n",
                    "tools": ["n8n", "API WB"],
                    "time_saved_hours_per_day": 1.5,
                    "implementation_complexity": "medium",
                }
            ],
            "roi_calculation": {
                "total_time_saved_hours_per_month": 30.0,
                "cost_saved_per_month_rub": 30000.0,
                "implementation_cost_rub": 50000.0,
                "payback_period_months": 1.7,
            },
            "proposal_structure": ["Слайд 1: Проблема", "Слайд 2: Решение"],
            "notes": "Данные по ROI оценочные",
        },
        ArchitectResponse: {
            "summary": "Автоматизация через n8n + Bpium",
            "approach": "Webhook → n8n → фильтрация → Bpium",
            "systems": [
                {
                    "name": "Wildberries API",
                    "role": "source",
                    "api_available": True,
                    "limitations": "Rate limit: 10 req/sec",
                }
            ],
            "data_flow": [
                {
                    "step": 1,
                    "from": "Wildberries",
                    "to": "n8n",
                    "trigger": "cron (каждые 15 минут)",
                    "data": "Список новых отзывов",
                    "transformation": "Фильтрация по рейтингу < 4",
                }
            ],
            "tech_stack": ["n8n", "Bpium", "API WB"],
            "estimated_complexity": "medium",
            "estimated_time_hours": 8,
            "risks": ["Изменение API WB"],
            "recommendations": "Добавить логирование всех запросов",
        },
        DeveloperResponse: {
            "summary": "Создан workflow для обработки заявок из Telegram",
            "workflow_name": "Telegram → Bpium заявки",
            "n8n_json": {"name": "Telegram → Bpium", "nodes": [], "connections": {}},
            "files": [
                {
                    "name": "telegram-bpium-workflow.json",
                    "type": "n8n_workflow",
                    "description": "Готовый workflow для импорта в n8n",
                }
            ],
            "setup_instructions": [
                "Импортировать workflow в n8n",
                "Настроить credentials для Telegram Bot API",
            ],
            "testing_steps": [
                "Отправить тестовое сообщение в бот",
                "Проверить запись в Bpium",
            ],
            "notes": None,
        },
        QAResponse: {
            "summary": "Результат соответствует задаче. Архитектура корректна.",
            "tests_total": 4,
            "tests_passed": 4,
            "tests_failed": 0,
            "issues": [],
            "warnings": ["Рекомендуем добавить мониторинг"],
            "recommendations": ["Добавить логирование"],
            "test_cases": [
                {
                    "name": "Соответствие задаче",
                    "status": "passed",
                    "description": "Архитектура решает поставленную задачу",
                }
            ],
        },
        TechWriterResponse: {
            "summary": "Создано пользовательское руководство и FAQ",
            "documents": [
                {
                    "title": "Руководство пользователя",
                    "type": "user_guide",
                    "audience": "Менеджеры клиента",
                    "sections": [
                        {
                            "title": "Начало работы",
                            "content": "Описание первых шагов...",
                            "screenshot_needed": True,
                            "screenshot_description": "Главный экран системы",
                        }
                    ],
                }
            ],
            "video_scripts": [
                {
                    "title": "Обзор системы",
                    "duration_minutes": 3,
                    "script": "Добро пожаловать! В этом видео...",
                    "visual_cues": ["Показать главный экран"],
                }
            ],
            "faq": [
                {
                    "question": "Что делать если автоматизация не сработала?",
                    "answer": "Проверьте логи в n8n...",
                }
            ],
            "checklist": ["Проверить credentials", "Запустить тестовый сценарий"],
            "notes": None,
        },
        LeadHunterResponse: {
            "leads_found": [
                {
                    "company_name": "ООО Ромашка",
                    "marketplace": "Wildberries",
                    "category": "Одежда",
                    "estimated_revenue": None,
                    "pain_points": ["Ручная обработка отзывов"],
                    "contact_telegram": None,
                    "contact_email": None,
                    "contact_phone": None,
                    "website": "https://romashka-shop.example",
                    "source_url": "https://romashka-shop.example",
                    "source_query": "бренд одежды официальный сайт",
                    "source": "openserp:google",
                }
            ],
            "total_found": 1,
            "search_queries": ["бренд одежды официальный сайт"],
            "notes": "Найден 1 лид из OpenSERP",
        },
        ClientHunterResponse: {
            "summary": "Найдено 2 клиента через Google; подготовлены персональные УТП",
            "search_queries": [
                "селлер Wildberries автоматизация отзывов",
                "интернет-магазин CRM интеграция заявки",
            ],
            "clients": [
                {
                    "company_name": "ТехноФикс",
                    "website": "https://example.com",
                    "snippet": "Магазин запчастей, 500+ SKU",
                    "niche": "e-commerce / автозапчасти",
                    "pain_hypothesis": ["Ручной разбор заявок из мессенджеров"],
                    "usp": {
                        "headline": "Заявки из Telegram и сайта — в CRM за 1 день",
                        "value_proposition": (
                            "Автоматизируем сбор обращений и постановку задач "
                            "без найма операторов"
                        ),
                        "differentiators": [
                            "Готовый blueprint под ваш стек",
                            "Окупаемость < 2 месяцев",
                        ],
                        "call_to_action": "15-мин демо на ваших каналах",
                    },
                    "source": "google",
                    "source_query": "интернет-магазин CRM интеграция заявки",
                }
            ],
            "total_found": 1,
            "notes": "Только открытые результаты Google Custom Search",
            "handoff_to_sales": {
                "recommended_approach": "Короткое сообщение с УТП headline",
                "priority_clients": ["ТехноФикс"],
            },
        },
        SalesResponse: {
            "messages": [
                {
                    "lead_name": "ООО Ромашка",
                    "message_text": "Здравствуйте! Вижу, что вы активно продаёте на WB...",
                    "channel": "telegram",
                    "personalization_points": ["Активные продажи на WB", "Категория Одежда"],
                }
            ],
            "qualification_questions": [
                "Сколько времени уходит на обработку отзывов?",
                "Используете ли вы CRM?",
            ],
            "next_steps": "Назначить встречу с аналитиком для расчёта ROI",
        },
        CRMCustomizerResponse: {
            "summary": "Настроена воронка обработки заявок в Bpium",
            "platform": "Bpium",
            "entities": [
                {
                    "name": "Заявки",
                    "custom_fields": [
                        {
                            "name": "Источник",
                            "type": "select",
                            "purpose": "Канал поступления заявки",
                        }
                    ],
                }
            ],
            "pipelines": [
                {
                    "name": "Обработка заявок",
                    "stages": ["Новая", "В работе", "Завершена"],
                }
            ],
            "business_processes": [
                {
                    "trigger": "Создание новой заявки",
                    "actions": ["Отправить уведомление менеджеру"],
                    "purpose": "Автоматическое оповещение",
                }
            ],
            "field_mapping": [
                {
                    "from_system": "n8n",
                    "from_field": "source",
                    "to_system": "Bpium",
                    "to_field": "Источник",
                }
            ],
            "setup_steps": [
                "Создать таблицу 'Заявки' в Bpium",
                "Добавить кастомные поля",
            ],
            "notes": None,
        },
    }


def get_model_example(model_class: type) -> str:
    """
    Получает пример JSON для модели.
    Используется для вставки в промпты агентов.
    """
    global _MODEL_EXAMPLES
    if not _MODEL_EXAMPLES:
        _MODEL_EXAMPLES = _build_examples()

    example_data = _MODEL_EXAMPLES.get(model_class)
    if example_data is None:
        logger.warning(f"Нет примера для {model_class.__name__}")
        return "{}"

    try:
        return json.dumps(example_data, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"Не удалось сериализовать пример для {model_class.__name__}: {e}")
        return "{}"
        
def extract_json_from_text(text: str) -> str:
    """
    Извлекает JSON из текста ответа LLM.
    Обрабатывает markdown-обёртки, комментарии до/после JSON.
    """
    if not text or not text.strip():
        raise ValueError("Пустой ответ")
    
    text = text.strip()
    
    # Удаляем markdown-обёртки ```json ... ``` или ``` ... ```
    import re
    markdown_pattern = r'```(?:json)?\s*(.*?)\s*```'
    matches = re.findall(markdown_pattern, text, re.DOTALL)
    if matches:
        # Берём последний матч (обычно это основной JSON)
        text = matches[-1].strip()
    
    # Ищем первый { или [
    start_obj = text.find('{')
    start_arr = text.find('[')
    
    if start_obj == -1 and start_arr == -1:
        raise ValueError(f"JSON не найден в ответе. Текст: {text[:200]}...")
    
    # Определяем, что идёт первым
    if start_obj == -1:
        start = start_arr
        end_char = ']'
    elif start_arr == -1:
        start = start_obj
        end_char = '}'
    else:
        if start_obj < start_arr:
            start = start_obj
            end_char = '}'
        else:
            start = start_arr
            end_char = ']'
    
    # Ищем последний закрывающий символ
    end = text.rfind(end_char)
    
    if end == -1 or end <= start:
        raise ValueError(f"JSON не найден (нет закрывающей скобки {end_char}). Текст: {text[:200]}...")
    
    result = text[start:end+1]
    
    # Проверяем, что это валидный JSON
    try:
        json.loads(result)
        return result
    except json.JSONDecodeError as e:
        # Если не валидно, пробуем восстановить
        raise ValueError(f"Найденный текст не является валидным JSON: {result[:100]}... Ошибка: {e}")
        
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