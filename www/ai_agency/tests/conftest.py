"""
Общие фикстуры для тестов ИИ-агентства.
"""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime


# ==================== ФИКСТУРЫ ДАННЫХ ====================

@pytest.fixture
def mock_llm_response():
    """Типовой ответ YandexGPT."""
    return {
        "output_text": '{"status": "ok", "data": "test"}',
        "usage": {"total_tokens": 100}
    }


@pytest.fixture
def sample_project_data():
    """Тестовые данные проекта."""
    return {
        "Id": 1,
        "project_name": "Тестовый проект",
        "client_name": "ООО Тест",
        "goal": "Автоматизировать сбор заявок",
        "status": "in_progress",
        "tokens_used": 1000,
        "token_budget": 50000,
        "current_phase": "analysis",
        "final_report": "",
        "metrics": "{}",
        "completed_at": ""
    }


@pytest.fixture
def sample_task_data():
    """Тестовые данные задачи."""
    return {
        "Id": 100,
        "task_id": "task_001",
        "project_id": 1,
        "agent_name": "analyst",
        "task_description": "Провести анализ",
        "input_data": "{}",
        "status": "pending",
        "depends_on": "[]",
        "iteration_count": 0,
        "max_iterations": 3,
        "qa_approved": "pending",
        "qa_feedback": "",
        "tokens_used": 0
    }


@pytest.fixture
def sample_pydantic_responses():
    """Готовые Pydantic-модели для тестов."""
    from core.schemas import (
        PMDecision, PMTaskGraph, PMDecomposition, PMFinalReport,
        PMHumanReview, PMDeadlockResolution,
        AnalystResponse, ArchitectResponse, DeveloperResponse,
        QAResponse, TechWriterResponse,
        LeadHunterResponse, SalesResponse, CRMCustomizerResponse,
        PainPoint, ProposedAutomation, ROICalculation,
        QAIssue, QATestCase,
        SystemInfo, DataFlowStep,
        DeveloperFile,
        Document, DocumentSection, VideoScript, FAQItem,
        Lead, SalesMessage,
        CustomField, Entity, Pipeline, BusinessProcess, FieldMapping
    )
    
    return {
        "pm_decision": PMDecision(
            project_status="in_progress",
            current_phase="analysis",
            next_agent="analyst",
            task_for_next_agent="Провести анализ",
            pm_comment="Начинаем работу"
        ),
        "pm_task_graph": PMTaskGraph(
            tasks=[
                {"task_id": "task_001", "agent_name": "analyst", "depends_on": []},
                {"task_id": "task_002", "agent_name": "architect", "depends_on": ["task_001"]}
            ]
        ),
        "pm_decomposition": PMDecomposition(
            subtasks=[
                {"subtask_id": "dev_001", "description": "Создать webhook", "depends_on": []}
            ],
            pm_comment="Разбил на подзадачи"
        ),
        "pm_final_report": PMFinalReport(
            project_status="completed",
            final_report="# Отчёт\n\nПроект завершен.",
            metrics={"total_tokens_used": 10000, "tasks_completed": 5},
            pm_comment="Проект успешно завершен"
        ),
        "pm_human_review": PMHumanReview(
            updated_task_description="Новое описание",
            pm_comment="Задача скорректирована"
        ),
        "pm_deadlock": PMDeadlockResolution(
            analysis="Тупик",
            solution="update_dependencies",
            actions=[{"action": "update_task", "task_id": "task_001"}],
            comment="Обновлены зависимости"
        ),
        "analyst_response": AnalystResponse(
            client_name="ООО Тест",
            current_pain_points=[
                PainPoint(process="Ручной перенос", time_per_day_hours=2.0, cost_per_month_rub=20000.0)
            ],
            proposed_automation=[
                ProposedAutomation(
                    solution="Автоматизация",
                    tools=["n8n"],
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
            proposal_structure=["Слайд 1"],
            notes="Тест"
        ),
        "architect_response": ArchitectResponse(
            summary="Архитектура n8n + Bpium",
            approach="Webhook → n8n → Bpium",
            systems=[SystemInfo(name="WB API", role="source", api_available=True)],
            data_flow=[DataFlowStep(
                **{"step": 1, "from": "WB", "to": "n8n", "trigger": "cron", "data": "Отзывы", "transformation": "Фильтр"}
            )],
            tech_stack=["n8n"],
            estimated_complexity="medium",
            estimated_time_hours=8,
            risks=["Риск"],
            recommendations="Логировать"
        ),
        "developer_response": DeveloperResponse(
            summary="Создан workflow",
            workflow_name="Test",
            n8n_json={"name": "test"},
            files=[DeveloperFile(name="w.json", type="n8n_workflow", description="Workflow")],
            setup_instructions=["Шаг 1"],
            testing_steps=["Тест 1"],
            notes="Готово"
        ),
        "qa_response": QAResponse(
            summary="Проверка пройдена",
            tests_total=5,
            tests_passed=5,
            tests_failed=0,
            issues=[],
            warnings=[],
            recommendations=["Отлично"],
            test_cases=[QATestCase(name="Тест 1", status="passed", description="Проверка")]
        ),
        "tech_writer_response": TechWriterResponse(
            summary="Создана документация интеграции",
            documents=[Document(
                title="Документация интеграции",
                type="integration_guide",
                audience="Администратор",
                sections=[
                    DocumentSection(title="Цель интеграции", content="Сценарий передаёт события во внешний API получателя.", screenshot_needed=False),
                    DocumentSection(title="Источник данных и получатель", content="Источник webhook, получатель HTTP API сервиса.", screenshot_needed=False),
                    DocumentSection(title="Версия API", content="Работаем с API v1 получателя данных.", screenshot_needed=False),
                    DocumentSection(title="Способ получения событий (webhook)", content="Webhook выбран для near-realtime событий.", screenshot_needed=False),
                    DocumentSection(title="Лимиты и постраничная выдача", content="Учтён rate limit; пагинация для webhook не нужна.", screenshot_needed=False),
                    DocumentSection(title="Критичные поля", content="event_id обязателен, имя поля менять нельзя.", screenshot_needed=False),
                    DocumentSection(title="Контракт данных", content="JSON с event_id и payload; без event_id — стоп.", screenshot_needed=False),
                    DocumentSection(title="Обработка ошибок", content="503 — retry; 401 — permanent + алерт в Telegram.", screenshot_needed=False),
                    DocumentSection(title="Адаптер / нормализация", content="Set-нода нормализует payload до контракта.", screenshot_needed=False),
                    DocumentSection(title="Тестирование", content="Проверить успех, 503, дубль event_id.", screenshot_needed=False),
                    DocumentSection(title="Сопровождение", content="Ответственный — команда интеграции клиента.", screenshot_needed=False),
                ],
            )],
            video_scripts=[VideoScript(title="Видео", duration_minutes=5, script="Текст", visual_cues=["Экран"])],
            faq=[FAQItem(question="Что при 401?", answer="Проверить токен, смотреть журнал.")],
            checklist=[
                "Контракт данных",
                "Критичные поля",
                "Ошибки",
                "Дедуп",
                "Алерт",
            ],
            notes="Готово",
        ),
        "lead_hunter_response": LeadHunterResponse(
            leads_found=[Lead(
                company_name="ООО Ромашка",
                marketplace="WB",
                category="Одежда",
                pain_points=["Много отзывов"],
                source="Telegram"
            )],
            total_found=1,
            notes="Найден 1 лид"
        ),
        "sales_response": SalesResponse(
            messages=[SalesMessage(
                lead_name="ООО Ромашка",
                message_text="Здравствуйте!",
                channel="telegram",
                personalization_points=["Активные продажи"]
            )],
            qualification_questions=["Вопрос 1"],
            next_steps="Назначить встречу"
        ),
        "crm_customizer_response": CRMCustomizerResponse(
            summary="Настроена CRM",
            platform="Bpium",
            entities=[Entity(name="Заявки", custom_fields=[
                CustomField(name="Источник", type="select", purpose="Канал")
            ])],
            pipelines=[Pipeline(name="Обработка", stages=["Новая", "В работе"])],
            business_processes=[BusinessProcess(trigger="Создание", actions=["Уведомить"], purpose="Оповещение")],
            field_mapping=[FieldMapping(from_system="n8n", from_field="source", to_system="Bpium", to_field="Источник")],
            setup_steps=["Шаг 1"],
            notes="Готово"
        )
    }


# ==================== ФИКСТУРЫ МОКОВ ====================

@pytest.fixture
def mock_nocodb_clients():
    """Автоматически мокает клиенты NocoDB."""
    with patch('core.nocodb.NocoDBClient') as mock_nocodb, \
         patch('core.nocodb.ProjectsClient') as mock_projects, \
         patch('core.nocodb.TasksClient') as mock_tasks:
        
        mock_nocodb_instance = MagicMock()
        mock_projects_instance = MagicMock()
        mock_tasks_instance = MagicMock()
        
        mock_nocodb.return_value = mock_nocodb_instance
        mock_projects.return_value = mock_projects_instance
        mock_tasks.return_value = mock_tasks_instance
        
        yield {
            'nocodb': mock_nocodb_instance,
            'projects': mock_projects_instance,
            'tasks': mock_tasks_instance
        }


@pytest.fixture
def orchestrator(mock_nocodb_clients):
    """Создаёт Orchestrator с внедрёнными моками."""
    from core.orchestrator import Orchestrator
    
    orch = Orchestrator()
    orch.projects_db = mock_nocodb_clients['projects']
    orch.tasks_db = mock_nocodb_clients['tasks']
    orch.agent_logs_db = mock_nocodb_clients['nocodb']
    return orch