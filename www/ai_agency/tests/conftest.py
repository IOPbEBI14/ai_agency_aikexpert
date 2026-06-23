import pytest
from unittest.mock import MagicMock, patch
from core.orchestrator import Orchestrator

@pytest.fixture
def mock_nocodb_clients():
    """Автоматически мокает клиенты NocoDB."""
    with patch('core.nocodb.NocoDBClient') as mock_nocodb, \
         patch('core.nocodb.ProjectsClient') as mock_projects, \
         patch('core.nocodb.TasksClient') as mock_tasks:
        
        yield {
            'nocodb': mock_nocodb.return_value,
            'projects': mock_projects.return_value,
            'tasks': mock_tasks.return_value
        }

@pytest.fixture
def orchestrator(mock_nocodb_clients):
    """Создаёт Orchestrator с внедрёнными моками."""
    orch = Orchestrator()
    # ⭐ ВАЖНО: Принудительно подменяем клиенты на моки
    orch.projects_db = mock_nocodb_clients['projects']
    orch.tasks_db = mock_nocodb_clients['tasks']
    orch.agent_logs_db = mock_nocodb_clients['nocodb']
    return orch

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
        "tokens_used": 0,
        "token_budget": 50000,
        "current_phase": "analysis"
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
        "tokens_used": 0
    }

@pytest.fixture
def sample_pydantic_responses():
    """Готовые Pydantic-модели для тестов."""
    from core.schemas import (
        PMDecision, AnalystResponse, QAResponse,
        PainPoint, ProposedAutomation, ROICalculation,
        QAIssue, QATestCase
    )
    return {
        "pm_decision": PMDecision(
            project_status="in_progress",
            current_phase="analysis",
            next_agent="analyst",
            task_for_next_agent="Провести анализ",
            pm_comment="Начинаем работу"
        ),
        "analyst_response": AnalystResponse(
            client_name="ООО Тест",
            current_pain_points=[
                PainPoint(process="Ручной перенос", time_per_day_hours=2.0, cost_per_month_rub=20000.0)
            ],
            proposed_automation=[
                ProposedAutomation(solution="Автоматизация", tools=["n8n"], time_saved_hours_per_day=1.5, implementation_complexity="medium")
            ],
            roi_calculation=ROICalculation(total_time_saved_hours_per_month=30.0, cost_saved_per_month_rub=30000.0, implementation_cost_rub=50000.0, payback_period_months=1.7),
            proposal_structure=["Слайд 1"],
            notes="Тест"
        ),
        "qa_response": QAResponse(
            summary="Проверка пройдена",
            tests_total=5,
            tests_passed=5,
            tests_failed=0,
            issues=[],
            warnings=[],
            recommendations=[],
            test_cases=[QATestCase(name="Тест 1", status="passed", description="Проверка")]
        )
    }    