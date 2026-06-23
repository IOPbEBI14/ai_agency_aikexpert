"""
Тесты для методов Orchestrator.
"""
import pytest
from unittest.mock import patch, MagicMock
from core.utils import call_llm, try_fix_truncated_json, validate_with_qa
import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from core.orchestrator import Orchestrator


class TestOrchestratorInitialization:
    """Тесты инициализации Orchestrator."""
    
    def test_orchestrator_creation(self, orchestrator):
        """Тест создания Orchestrator."""
        assert orchestrator.current_project is None
        assert orchestrator.agency_running is False
    
    def test_initialize_with_project_id(self, orchestrator, mock_nocodb_clients, sample_project_data):
        """Тест инициализации с ID проекта."""
        mock_nocodb_clients['projects'].find_project_by_id.return_value = sample_project_data
        
        result = orchestrator.initialize(project_id=1)
        
        assert result is True
        assert orchestrator.current_project == sample_project_data
        mock_nocodb_clients['projects'].find_project_by_id.assert_called_once_with(1)
    
    def test_initialize_with_nonexistent_project(self, orchestrator, mock_nocodb_clients):
        """Тест инициализации с несуществующим проектом."""
        mock_nocodb_clients['projects'].find_project_by_id.return_value = None
        
        result = orchestrator.initialize(project_id=999)
        
        assert result is False
    
    def test_initialize_finds_active_project(self, orchestrator, mock_nocodb_clients, sample_project_data):
        """Тест поиска активного проекта."""
        mock_nocodb_clients['projects'].find_project_by_status.return_value = sample_project_data
        
        result = orchestrator.initialize()
        
        assert result is True
        mock_nocodb_clients['projects'].find_project_by_status.assert_called_with("in_progress")
    
    def test_initialize_resumes_stopped_project(self, orchestrator, mock_nocodb_clients, sample_project_data):
        """Тест возобновления остановленного проекта."""
        # Нет активных проектов, есть остановленный
        mock_nocodb_clients['projects'].find_project_by_status.side_effect = [
            None,  # in_progress
            sample_project_data  # stopped
        ]
        
        result = orchestrator.initialize()
        
        assert result is True
        mock_nocodb_clients['projects'].update_project.assert_called_once()
    
    def test_initialize_creates_new_project(self, orchestrator, mock_nocodb_clients, sample_project_data):
        """Тест создания нового проекта."""
        # Нет ни активных, ни остановленных проектов
        mock_nocodb_clients['projects'].find_project_by_status.return_value = None
        mock_nocodb_clients['projects'].create_project.return_value = sample_project_data
        
        result = orchestrator.initialize()
        
        assert result is True
        mock_nocodb_clients['projects'].create_project.assert_called_once()

class TestOrchestratorRun:
    """Тесты метода run()."""
    
    @patch.object(Orchestrator, 'execute_task')
    def test_run_executes_tasks(self, mock_execute, orchestrator, mock_nocodb_clients, sample_project_data):
        """Тест выполнения задач."""
        orchestrator.current_project = sample_project_data
        
        # Мок возвращает задачи
        mock_nocodb_clients['tasks'].get_tasks_by_project.return_value = [
            {"task_id": "task_001", "status": "pending", "depends_on": "[]"},
            {"task_id": "task_002", "status": "completed", "depends_on": '["task_001"]'}
        ]
        
        mock_execute.return_value = True
        
        # Запускаем на 1 итерацию
        orchestrator.MAX_TOTAL_ITERATIONS = 1
        orchestrator.run()
        
        # Проверяем, что execute_task был вызван
        mock_execute.assert_called()
    
    def test_run_stops_on_budget_exhaustion(self, orchestrator, mock_nocodb_clients, sample_project_data):
        """Тест остановки при исчерпании бюджета."""
        # Бюджет почти исчерпан
        sample_project_data["tokens_used"] = 49000
        sample_project_data["token_budget"] = 50000
        orchestrator.current_project = sample_project_data
        
        mock_nocodb_clients['tasks'].get_tasks_by_project.return_value = []
        
        orchestrator.MAX_TOTAL_ITERATIONS = 1
        orchestrator.run()
        
        # Проверяем, что проект переведён в needs_human_review
        mock_nocodb_clients['projects'].update_project.assert_called()

class TestOrchestratorExecuteTask:
    """Тесты метода execute_task()."""
    
    @patch('main.load_prompt')
    @patch('main.call_llm')
    @patch('main.log_to_agent_logs')
    @patch('main.update_last_agent_log')
    def test_execute_analyst_task(self, mock_update_log, mock_log, mock_call_llm, mock_load_prompt,
                                  mock_nocodb_clients, sample_project_data, sample_task_data):
        """Тест выполнения задачи аналитика."""
        orchestrator = Orchestrator()
        orchestrator.current_project = sample_project_data
        
        # Mock возвращает валидный JSON, соответствующий AnalystResponse
        valid_analyst_json = '''
        {
            "client_name": "Тест",
            "current_pain_points": [
                {
                    "process": "Ручной перенос",
                    "time_per_day_hours": 2.0,
                    "cost_per_month_rub": 20000.0
                }
            ],
            "proposed_automation": [
                {
                    "solution": "Автоматизация",
                    "tools": ["n8n"],
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
            "proposal_structure": ["Слайд 1"],
            "notes": "Тест"
        }
        '''
        
        mock_call_llm.return_value = (valid_analyst_json, 100)
        mock_load_prompt.return_value = "Промпт аналитика"
        
        # Mock QA
        with patch.object(orchestrator, 'run_qa_gate', return_value=True):
            result = orchestrator.execute_task(sample_task_data, "pm_prompt")
        
        assert result is True
        mock_call_llm.assert_called_once()
    
    def test_execute_task_exceeds_iterations(self, mock_nocodb_clients, sample_project_data, sample_task_data):
        """Тест превышения лимита итераций."""
        orchestrator = Orchestrator()
        orchestrator.current_project = sample_project_data
        
        sample_task_data["iteration_count"] = 3
        sample_task_data["max_iterations"] = 3
        
        result = orchestrator.execute_task(sample_task_data, "pm_prompt")
        
        assert result is False
        mock_nocodb_clients['tasks'].update_task.assert_called_with(
            sample_task_data["Id"],
            {"status": "failed", "qa_feedback": "Превышен лимит итераций"}
        )


class TestOrchestratorQA:class TestOrchestratorQA:
    """Тесты метода run_qa_gate()."""
    
    @patch('core.utils.validate_with_qa')
    @patch('core.utils.log_to_agent_logs')
    @patch('core.utils.update_last_agent_log')
    def test_qa_passed(self, mock_update_log, mock_log, mock_validate_qa,
                       orchestrator, mock_nocodb_clients, sample_project_data, sample_task_data):
        """Тест пройденной QA проверки."""
        orchestrator.current_project = sample_project_data
        
        mock_validate_qa.return_value = {
            "approved": True,
            "feedback": "Всё отлично",
            "tokens_used": 50
        }
        
        result = orchestrator.run_qa_gate(
            sample_task_data,
            sample_task_data["Id"],
            "task_001",
            "analyst",
            '{"response": "data"}',
            "Описание задачи",
            0,
            3
        )
        
        assert result is True
        
        call_args = mock_nocodb_clients['tasks'].update_task.call_args
        assert call_args[0][1]["status"] == "completed"
        assert call_args[0][1]["qa_approved"] == "true"
    
    @patch('core.utils.validate_with_qa')
    @patch('core.utils.log_to_agent_logs')
    @patch('core.utils.update_last_agent_log')
    def test_qa_failed(self, mock_update_log, mock_log, mock_validate_qa,
                       orchestrator, mock_nocodb_clients, sample_project_data, sample_task_data):
        """Тест проваленной QA проверки."""
        orchestrator.current_project = sample_project_data
        
        mock_validate_qa.return_value = {
            "approved": False,
            "feedback": "Найдены ошибки",
            "tokens_used": 50
        }
        
        result = orchestrator.run_qa_gate(
            sample_task_data,
            sample_task_data["Id"],
            "task_001",
            "analyst",
            '{"response": "data"}',
            "Описание задачи",
            0,
            3
        )
        
        assert result is False
        
        call_args = mock_nocodb_clients['tasks'].update_task.call_args
        assert call_args[0][1]["status"] == "pending"
        assert call_args[0][1]["qa_approved"] == "false"
        assert call_args[0][1]["qa_feedback"] == "Найдены ошибки"
    
    @patch('core.utils.validate_with_qa')
    @patch('core.utils.log_to_agent_logs')
    @patch('core.utils.update_last_agent_log')
    def test_qa_failed_max_iterations(self, mock_update_log, mock_log, mock_validate_qa,
                                      orchestrator, mock_nocodb_clients, sample_project_data, sample_task_data):
        """Тест проваленной QA после максимального числа итераций."""
        orchestrator.current_project = sample_project_data
        
        mock_validate_qa.return_value = {
            "approved": False,
            "feedback": "Ошибки",
            "tokens_used": 50
        }
        
        sample_task_data["iteration_count"] = 2
        sample_task_data["max_iterations"] = 3
        
        result = orchestrator.run_qa_gate(
            sample_task_data,
            sample_task_data["Id"],
            "task_001",
            "analyst",
            '{"response": "data"}',
            "Описание задачи",
            2,
            3
        )
        
        assert result is False
        mock_nocodb_clients['tasks'].update_task.assert_called_with(
            sample_task_data["Id"],
            {"status": "failed"}
        )
        
class TestOrchestratorHelpers:
    """Тесты вспомогательных методов."""
    
    def test_find_ready_tasks(self, mock_nocodb_clients):
        """Тест поиска готовых задач."""
        orchestrator = Orchestrator()
        
        pending_tasks = [
            {"task_id": "task_001", "depends_on": "[]"},
            {"task_id": "task_002", "depends_on": '["task_001"]'},
            {"task_id": "task_003", "depends_on": '["task_999"]'}
        ]
        
        completed_ids = ["task_001"]
        
        ready = orchestrator._find_ready_tasks(pending_tasks, completed_ids)
        
        assert len(ready) == 2
        assert ready[0]["task_id"] == "task_001"
        assert ready[1]["task_id"] == "task_002"
    
    def test_expand_completed_with_parents(self, mock_nocodb_clients):
        """Тест расширения completed ID родителями."""
        orchestrator = Orchestrator()
        
        tasks = [
            {"task_id": "dev_001", "status": "completed"},
            {"task_id": "dev_002", "status": "completed"},
            {"task_id": "task_003", "status": "in_progress"}
        ]
        
        completed_ids = ["dev_001", "dev_002"]
        
        expanded = orchestrator._expand_completed_with_parents(tasks, completed_ids)
        
        assert "task_003" in expanded
    
    def test_check_and_complete_parent_tasks(self, mock_nocodb_clients):
        """Тест завершения родительских задач."""
        orchestrator = Orchestrator()
        
        tasks = [
            {"task_id": "dev_001", "status": "completed", "Id": 101},
            {"task_id": "dev_002", "status": "completed", "Id": 102},
            {"task_id": "task_003", "status": "in_progress", "Id": 100}
        ]
        
        orchestrator.check_and_complete_parent_tasks(tasks)
        
        # Проверяем, что родительская задача обновлена
        mock_nocodb_clients['tasks'].update_task.assert_called_with(
            100,
            {
                "status": "completed",
                "qa_approved": "true",
                "qa_feedback": "Все 2 подзадач завершены успешно"
            }
        )