"""
Общие фикстуры и моки для тестов.
"""
import pytest
import json
import os
import sys
from unittest.mock import MagicMock, patch
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

@pytest.fixture(autouse=True)
def mock_nocodb_clients():
    """Автоматически мокает клиенты NocoDB для всех тестов."""
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
def mock_llm():
    """Мокает вызовы LLM."""
    with patch('core.orchestrator.call_llm') as mock_call:
        mock_call.return_value = ('{"status": "ok"}', 100)
        yield mock_call

@pytest.fixture
def sample_project_data():
    """Пример данных проекта."""
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
    """Пример данных задачи."""
    return {
        "Id": 100,
        "task_id": "task_001",
        "project_id": 1,
        "agent_name": "analyst",
        "task_description": "Провести анализ потребностей клиента",
        "input_data": "{}",
        "status": "pending",
        "depends_on": "[]",
        "iteration_count": 0,
        "max_iterations": 3,
        "qa_approved": "pending",
        "qa_feedback": "",
        "tokens_used": 0,
        "created_at": datetime.now().isoformat()
    }


@pytest.fixture
def mock_llm_response():
    """Пример ответа LLM."""
    return {
        "output_text": '{"project_status": "in_progress", "current_phase": "analysis", "next_agent": "analyst", "task_for_next_agent": "Провести анализ", "pm_comment": "Начинаем работу"}',
        "usage": {
            "total_tokens": 100
        }
    }


@pytest.fixture
def sample_pydantic_responses():
    """Примеры валидных ответов для Pydantic моделей."""
    return {
        "pm_decision": {
            "project_status": "in_progress",
            "current_phase": "analysis",
            "next_agent": "analyst",
            "task_for_next_agent": "Провести анализ потребностей",
            "pm_comment": "Начинаем с анализа"
        },
        "analyst_response": {
            "client_name": "ООО Тест",
            "current_pain_points": [
                {
                    "process": "Ручной перенос данных",
                    "time_per_day_hours": 2.0,
                    "cost_per_month_rub": 20000.0
                }
            ],
            "proposed_automation": [
                {
                    "solution": "Автоматическая синхронизация",
                    "tools": ["n8n", "API WB"],
                    "time_saved_hours_per_day": 1.5,
                    "implementation_complexity": "medium"
                }
            ],
            "roi_calculation": {
                "total_time_saved_hours_per_month": 30.0,
                "cost_saved_per_month_rub": 30000.0,
                "implementation_cost_rub": 50000.0,
                "payback_period_months": 1.7
            },
            "proposal_structure": [
                "Слайд 1: Проблема",
                "Слайд 2: Решение"
            ],
            "notes": "Тестовый пример"
        },
        "qa_response": {
            "summary": "Проверка пройдена",
            "tests_total": 5,
            "tests_passed": 5,
            "tests_failed": 0,
            "issues": [],
            "warnings": [],
            "recommendations": ["Всё отлично"],
            "test_cases": [
                {
                    "name": "Тест 1",
                    "status": "passed",
                    "description": "Проверка структуры"
                }
            ]
        }
    }