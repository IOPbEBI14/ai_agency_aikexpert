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

    def test_initialize_create_from_form(self, orchestrator, mock_nocodb_clients, sample_project_data):
        """Создание проекта из полей формы (create=...)."""
        active = {**sample_project_data, "Id": 7, "status": "in_progress"}
        created = {
            **sample_project_data,
            "Id": 99,
            "project_name": "New Form Project",
            "client_name": "Form Client",
            "goal": "Goal from dashboard form with enough length",
            "current_phase": "sales",
            "token_budget": 12000,
        }
        mock_nocodb_clients["projects"].find_project_by_status.return_value = active
        mock_nocodb_clients["projects"].create_project.return_value = created

        result = orchestrator.initialize(
            create={
                "project_name": "New Form Project",
                "client_name": "Form Client",
                "goal": "Goal from dashboard form with enough length",
                "token_budget": 12000,
                "current_phase": "sales",
            }
        )

        assert result is True
        assert orchestrator.current_project["Id"] == 99
        mock_nocodb_clients["projects"].update_project.assert_called_with(
            7, {"status": "stopped"}
        )
        mock_nocodb_clients["projects"].create_project.assert_called_once_with(
            project_name="New Form Project",
            client_name="Form Client",
            goal="Goal from dashboard form with enough length",
            token_budget=12000,
            current_phase="sales",
        )

    def test_initialize_create_rejects_incomplete(self, orchestrator, mock_nocodb_clients):
        result = orchestrator.initialize(
            create={"project_name": "X", "client_name": "", "goal": "long enough goal text"}
        )
        assert result is False
        mock_nocodb_clients["projects"].create_project.assert_not_called()

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
    
    @patch("core.orchestrator.log_to_agent_logs")
    def test_run_stops_on_budget_exhaustion(
        self, mock_log, orchestrator, mock_nocodb_clients, sample_project_data
    ):
        """При исчерпании бюджета проект → stopped (не needs_human_review)."""
        sample_project_data["tokens_used"] = 49000
        sample_project_data["token_budget"] = 50000
        orchestrator.current_project = sample_project_data

        mock_nocodb_clients["tasks"].get_tasks_by_project.return_value = []

        orchestrator.MAX_TOTAL_ITERATIONS = 1
        orchestrator.run()

        assert orchestrator.current_project["status"] == "stopped"
        stopped_updates = [
            c.args[1]
            for c in mock_nocodb_clients["projects"].update_project.call_args_list
            if len(c.args) > 1 and isinstance(c.args[1], dict) and c.args[1].get("status") == "stopped"
        ]
        assert stopped_updates, "ожидался update_project(..., status=stopped)"
        assert mock_log.called
        # status= передаётся keyword-only в log_to_agent_logs
        assert mock_log.call_args.kwargs.get("status") == "stopped"

class TestOrchestratorExecuteTask:
    """Тесты метода execute_task()."""
    
    @patch('core.task_executor.load_prompt')
    @patch('core.task_executor.call_llm')
    @patch('core.task_executor.log_to_agent_logs')
    @patch('core.task_executor.update_last_agent_log')
    def test_execute_analyst_task(self, mock_update_log, mock_log, mock_call_llm, mock_load_prompt,
                                  mock_nocodb_clients, sample_project_data, sample_task_data):
        """Тест выполнения задачи аналитика.
        
        Патчим core.task_executor.call_llm (а не core.utils.call_llm), потому что
        task_executor.py импортирует call_llm напрямую через 'from .utils import call_llm'.
        """
        orchestrator = Orchestrator()
        orchestrator.current_project = sample_project_data

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

        # run_qa_gate не вызывается для analyst (спец-обработчик),
        # но патчим на всякий случай
        with patch.object(orchestrator, 'run_qa_gate', return_value=True):
            result = orchestrator.execute_task(sample_task_data, "pm_prompt")

        assert result is True
        mock_call_llm.assert_called_once()
    
    def test_execute_task_exceeds_iterations(self, mock_nocodb_clients, sample_project_data, sample_task_data):
        """Тест превышения лимита итераций."""
        orchestrator = Orchestrator()
        orchestrator.current_project = sample_project_data
        
        # ⭐ ВАЖНО: Подменяем tasks_db на мок
        orchestrator.tasks_db = mock_nocodb_clients['tasks']
        
        # Устанавливаем лимит итераций
        sample_task_data['iteration_count'] = 3
        sample_task_data['max_iterations'] = 3
        sample_task_data['Id'] = 100  # Убеждаемся, что Id = 100
        
        result = orchestrator.execute_task(sample_task_data, "pm_prompt")
        
        assert result is False
        
        # Проверяем вызов update_task с правильными параметрами
        mock_nocodb_clients['tasks'].update_task.assert_called_once_with(
            100,
            {'status': 'failed', 'qa_feedback': 'Превышен лимит итераций'}
        )

class TestOrchestratorQA:
    """Тесты метода run_qa_gate() — использует call_and_parse_llm → call_llm."""

    # QAResponse JSON, имитирующий успешную проверку (tests_failed=0, нет critical-issues)
    _QA_PASSED_JSON = '''{
        "summary": "Проверка пройдена",
        "tests_total": 3,
        "tests_passed": 3,
        "tests_failed": 0,
        "issues": [],
        "warnings": [],
        "recommendations": [],
        "test_cases": []
    }'''

    # QAResponse JSON, имитирующий провал (tests_failed=1, есть high-issue)
    _QA_FAILED_JSON = '''{
        "summary": "Найдены ошибки",
        "tests_total": 3,
        "tests_passed": 1,
        "tests_failed": 2,
        "issues": [
            {
                "severity": "high",
                "type": "logic",
                "description": "Найдены ошибки",
                "location": "output",
                "recommendation": "Исправить"
            }
        ],
        "warnings": [],
        "recommendations": [],
        "test_cases": []
    }'''

    @patch('core.qa_gate.call_llm')
    @patch('core.qa_gate.log_to_agent_logs')
    @patch('core.qa_gate.update_last_agent_log')
    def test_qa_passed(self, mock_update_log, mock_log, mock_call_llm,
                       orchestrator, mock_nocodb_clients, sample_project_data, sample_task_data):
        """Тест пройденной QA проверки.
        
        Патчим core.qa_gate.call_llm — именно там выполняется вызов LLM.
        """
        orchestrator.current_project = sample_project_data
        mock_call_llm.return_value = (self._QA_PASSED_JSON, 50)

        result = orchestrator.run_qa_gate(
            sample_task_data,
            sample_task_data["Id"],
            "task_001",
            "analyst",
            '{"response": "data"}',
            "Описание задачи",
            0,
            3,
        )

        assert result is True

        call_args = mock_nocodb_clients['tasks'].update_task.call_args
        assert call_args[0][1]["status"] == "completed"
        assert call_args[0][1]["qa_approved"] == "true"

    @patch('core.qa_gate.call_llm')
    @patch('core.qa_gate.log_to_agent_logs')
    @patch('core.qa_gate.update_last_agent_log')
    def test_qa_failed(self, mock_update_log, mock_log, mock_call_llm,
                       orchestrator, mock_nocodb_clients, sample_project_data, sample_task_data):
        """Тест проваленной QA проверки."""
        orchestrator.current_project = sample_project_data
        mock_call_llm.return_value = (self._QA_FAILED_JSON, 50)

        result = orchestrator.run_qa_gate(
            sample_task_data,
            sample_task_data["Id"],
            "task_001",
            "analyst",
            '{"response": "data"}',
            "Описание задачи",
            0,
            3,
        )

        assert result is False

        call_args = mock_nocodb_clients['tasks'].update_task.call_args
        assert call_args[0][1]["status"] == "pending"
        assert call_args[0][1]["qa_approved"] == "false"
        # Feedback содержит summary + issue description
        assert "Найдены ошибки" in call_args[0][1]["qa_feedback"]

    @patch('core.qa_gate.call_llm')
    @patch('core.qa_gate.log_to_agent_logs')
    @patch('core.qa_gate.update_last_agent_log')
    def test_qa_failed_max_iterations(self, mock_update_log, mock_log, mock_call_llm,
                                      orchestrator, mock_nocodb_clients, sample_project_data, sample_task_data):
        """Тест проваленной QA после максимального числа итераций → status=failed."""
        orchestrator.current_project = sample_project_data
        mock_call_llm.return_value = (self._QA_FAILED_JSON, 50)

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
            3,
        )

        assert result is False
        mock_nocodb_clients['tasks'].update_task.assert_called_with(
            sample_task_data["Id"],
            {"status": "failed"},
        )
        
# class TestOrchestratorHelpers:
    # """Тесты вспомогательных методов."""
    
    # def test_find_ready_tasks(self, mock_nocodb_clients):
        # """Тест поиска готовых задач."""
        # orchestrator = Orchestrator()
        
        # pending_tasks = [
            # {"task_id": "task_001", "depends_on": "[]"},
            # {"task_id": "task_002", "depends_on": '["task_001"]'},
            # {"task_id": "task_003", "depends_on": '["task_999"]'}
        # ]
        
        # completed_ids = ["task_001"]
        
        # ready = orchestrator._find_ready_tasks(pending_tasks, completed_ids)
        
        # assert len(ready) == 2
        # assert ready[0]["task_id"] == "task_001"
        # assert ready[1]["task_id"] == "task_002"
    
    # def test_expand_completed_with_parents(self, mock_nocodb_clients):
        # """Тест расширения completed ID родителями."""
        # orchestrator = Orchestrator()
        
        # tasks = [
            # {"task_id": "dev_001", "status": "completed"},
            # {"task_id": "dev_002", "status": "completed"},
            # {"task_id": "task_003", "status": "in_progress"}
        # ]
        
        # completed_ids = ["dev_001", "dev_002"]
        
        # expanded = orchestrator._expand_completed_with_parents(tasks, completed_ids)
        
        # assert "task_003" in expanded
    
    # def test_check_and_complete_parent_tasks(self, mock_nocodb_clients):
        # """Тест завершения родительских задач."""
        # orchestrator = Orchestrator()
        
        # tasks = [
            # {"task_id": "dev_001", "status": "completed", "Id": 101},
            # {"task_id": "dev_002", "status": "completed", "Id": 102},
            # {"task_id": "task_003", "status": "in_progress", "Id": 100}
        # ]
        
        # orchestrator.check_and_complete_parent_tasks(tasks)
        
        # # Проверяем, что родительская задача обновлена
        # mock_nocodb_clients['tasks'].update_task.assert_called_with(
            # 100,
            # {
                # "status": "completed",
                # "qa_approved": "true",
                # "qa_feedback": "Все 2 подзадач завершены успешно"
            # }
        # )
        
class TestOrchestratorNewAgents:
    """Тесты для методов обработки новых агентов."""
    
    @patch('core.agent_handlers.log_to_agent_logs')
    def test_handle_lead_hunter(self, mock_log, orchestrator, mock_nocodb_clients, sample_project_data, sample_task_data):
        """Тест обработки результата Lead Hunter."""
        from core.schemas import LeadHunterResponse, Lead
        
        orchestrator.current_project = sample_project_data
        
        # Создаём Pydantic-модель
        lead_response = LeadHunterResponse(
            leads_found=[
                Lead(
                    company_name="ООО Ромашка",
                    marketplace="WB",
                    category="Одежда",
                    pain_points=["Много отзывов"],
                    website="https://romashka.example",
                    source_url="https://romashka.example",
                    source="openserp:google",
                )
            ],
            total_found=1,
            notes="Найден 1 лид"
        )
        orchestrator.current_project["_lead_hunter_serp"] = [
            {
                "title": "ООО Ромашка",
                "link": "https://romashka.example",
                "snippet": "магазин",
                "query": "q",
            }
        ]

        result = orchestrator._handle_lead_hunter(
            sample_task_data,
            sample_task_data["Id"],
            "task_001",
            lead_response,
            "pm_prompt"
        )
        
        assert result is True
        assert "leads_context" in orchestrator.current_project
        assert len(orchestrator.current_project["leads_context"]) == 1
        assert orchestrator.current_project["leads_context"][0]["company_name"] == "ООО Ромашка"
    
    @patch('core.agent_handlers.log_to_agent_logs')
    def test_handle_sales(self, mock_log, orchestrator, mock_nocodb_clients, sample_project_data, sample_task_data):
        """Тест обработки результата Sales."""
        from core.schemas import SalesResponse, SalesMessage
        
        orchestrator.current_project = sample_project_data
        orchestrator.current_project["client_hunter_context"] = [
            {
                "company_name": "ООО Ромашка",
                "website": "https://romashka.example",
                "contact_email": "info@romashka.example",
            }
        ]

        sales_response = SalesResponse(
            messages=[
                SalesMessage(
                    lead_name="ООО Ромашка",
                    message_text="Здравствуйте!",
                    channel="telegram",
                    personalization_points=["Активные продажи"]
                )
            ],
            qualification_questions=["Вопрос 1"],
            next_steps="Назначить встречу"
        )
        
        result = orchestrator._handle_sales(
            sample_task_data,
            sample_task_data["Id"],
            "task_001",
            sales_response,
            "pm_prompt"
        )
        
        assert result is True
        assert "sales_context" in orchestrator.current_project
        assert len(orchestrator.current_project["sales_context"]) == 1
        assert len(orchestrator.current_project["sales_context"][0]["messages"]) == 1

    @patch('core.agent_handlers.log_to_agent_logs')
    def test_handle_sales_usp_without_hunter_keeps_letter(
        self, mock_log, orchestrator, mock_nocodb_clients, sample_project_data, sample_task_data
    ):
        """УТП→письмо клиенту проекта: без hunter не очищать messages в tasks."""
        from core.schemas import SalesResponse, SalesMessage
        import json

        orchestrator.current_project = sample_project_data
        orchestrator.current_project["client_name"] = "Губанова Елена Сергеевна"
        orchestrator.current_project.pop("client_hunter_context", None)
        orchestrator.current_project.pop("leads_context", None)
        orchestrator.current_project["analyst_context"] = {
            "client_name": "Губанова Елена Сергеевна",
        }

        sales_response = SalesResponse(
            messages=[
                SalesMessage(
                    lead_name="Губанова Е.С.",
                    message_text="Уважаемая Елена Сергеевна! Наше УТП…",
                    channel="email",
                    subject="УТП для маркетплейса",
                    personalization_points=["поддержка маркетплейса"],
                )
            ],
            qualification_questions=[],
            next_steps="Отправить письмо вручную",
        )

        result = orchestrator._handle_sales(
            sample_task_data,
            sample_task_data["Id"],
            "iter2_task_001",
            sales_response,
            "pm_prompt",
        )
        assert result is True
        msgs = orchestrator.current_project["sales_context"][-1]["messages"]
        assert len(msgs) == 1
        assert "УТП" in msgs[0]["message_text"]

        # tasks.output_data должен содержать письма (не пустой список)
        call_kwargs = None
        for c in orchestrator.tasks_db.update_task.call_args_list:
            args, kwargs = c
            payload = args[1] if len(args) > 1 else kwargs.get("data")
            if isinstance(payload, dict) and "output_data" in payload:
                call_kwargs = payload
        assert call_kwargs is not None
        saved = json.loads(call_kwargs["output_data"])
        assert len(saved["messages"]) == 1
        assert "Подготовлено 1 писем" in call_kwargs.get("qa_feedback", "")
    
    @patch('core.agent_handlers.log_to_agent_logs')
    def test_handle_analyst(self, mock_log, orchestrator, mock_nocodb_clients, sample_project_data, sample_task_data):
        """Тест обработки результата Analyst."""
        from core.schemas import AnalystResponse, PainPoint, ProposedAutomation, ROICalculation
        
        orchestrator.current_project = sample_project_data
        
        # Создаём Pydantic-модель
        analyst_response = AnalystResponse(
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
        )
        
        # Вызываем метод
        result = orchestrator._handle_analyst(
            sample_task_data,
            sample_task_data["Id"],
            "task_001",
            analyst_response,
            "pm_prompt"
        )
        
        assert result is True
        assert "analyst_context" in orchestrator.current_project
        assert orchestrator.current_project["analyst_context"]["client_name"] == "ООО Тест"
        assert orchestrator.current_project["analyst_context"]["roi_calculation"]["cost_saved_per_month_rub"] == 30000.0
    
    @patch('core.agent_handlers.log_to_agent_logs')
    def test_handle_lead_hunter_with_string(self, mock_log, orchestrator, mock_nocodb_clients, sample_project_data, sample_task_data):
        """Тест обработки Lead Hunter со строковым ответом."""
        import json
        
        orchestrator.current_project = sample_project_data
        
        # Передаём JSON-строку
        lead_json = json.dumps({
            "leads_found": [
                {
                    "company_name": "ООО Тест",
                    "marketplace": "WB",
                    "category": "Одежда",
                    "pain_points": ["Тест"],
                    "source": "Telegram"
                }
            ],
            "total_found": 1,
            "notes": "Тест"
        })
        
        # Вызываем метод
        result = orchestrator._handle_lead_hunter(
            sample_task_data,
            sample_task_data["Id"],
            "task_001",
            lead_json,
            "pm_prompt"
        )
        
        assert result is True
        assert "leads_context" in orchestrator.current_project
