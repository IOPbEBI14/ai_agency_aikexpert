"""
Тесты Направления E: Промпты и поведение агентов.

Покрывает:
  - Соответствие промптов Pydantic-схемам (обязательные поля, enum-значения)
  - PMDeadlockResolution: 'need_human_review' в Literal
  - handoff-поля в AnalystResponse / LeadHunterResponse
  - Контекстный поток analyst → architect (handoff_to_architect)
  - Контекстный поток lead_hunter → sales (handoff_to_sales)
  - Спец-хендлер handle_analyst сохраняет handoff в current_project
  - Спец-хендлер handle_lead_hunter сохраняет handoff в current_project
  - sales_prompt.txt содержит правильную идентичность агента
"""
import json
import os
import pytest
from unittest.mock import MagicMock, patch


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════

PROMPTS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "prompts"
)


def _read_prompt(name: str) -> str:
    path = os.path.join(PROMPTS_DIR, f"{name}_prompt.txt")
    with open(path, encoding="utf-8") as f:
        return f.read()


# ══════════════════════════════════════════════════════════════════
# Prompt identity
# ══════════════════════════════════════════════════════════════════

class TestPromptIdentities:
    """Каждый промпт должен описывать правильную роль агента."""

    def test_sales_prompt_is_sales_agent(self):
        """sales_prompt.txt не должен представляться как Analyst & Pitcher."""
        content = _read_prompt("sales")
        assert "E-com Analyst" not in content, (
            "sales_prompt.txt содержит скопированную идентичность Analyst — "
            "должна быть исправлена на Sales Agent."
        )
        assert "Sales Agent" in content or "sales" in content.lower()

    def test_analyst_prompt_is_analyst(self):
        content = _read_prompt("analyst")
        assert "аналитик" in content.lower() or "analyst" in content.lower()

    def test_architect_prompt_is_architect(self):
        content = _read_prompt("architect")
        assert "architect" in content.lower() or "архитект" in content.lower()

    def test_developer_prompt_is_developer(self):
        content = _read_prompt("developer")
        assert "developer" in content.lower() or "разработчик" in content.lower()


# ══════════════════════════════════════════════════════════════════
# Prompt ↔ Schema alignment
# ══════════════════════════════════════════════════════════════════

class TestPromptSchemaAlignment:
    """Промпты, содержащие вшитую JSON Schema, должны совпадать с Pydantic-моделями."""

    def test_analyst_prompt_contains_required_fields(self):
        """analyst_prompt содержит все обязательные поля AnalystResponse."""
        content = _read_prompt("analyst")
        for field in ("client_name", "current_pain_points", "roi_calculation",
                      "proposed_automation", "proposal_structure"):
            assert field in content, f"Поле '{field}' отсутствует в analyst_prompt.txt"

    def test_architect_prompt_contains_required_fields(self):
        content = _read_prompt("architect")
        for field in ("summary", "approach", "systems", "data_flow",
                      "tech_stack", "estimated_complexity", "estimated_time_hours",
                      "risks", "recommendations"):
            assert field in content, f"Поле '{field}' отсутствует в architect_prompt.txt"

    def test_developer_prompt_contains_required_fields(self):
        content = _read_prompt("developer")
        for field in ("summary", "files", "setup_instructions", "testing_steps"):
            assert field in content, f"Поле '{field}' отсутствует в developer_prompt.txt"

    def test_lead_hunter_prompt_contains_required_fields(self):
        content = _read_prompt("lead_hunter")
        for field in ("leads_found", "total_found", "company_name",
                      "marketplace", "pain_points", "source"):
            assert field in content, f"Поле '{field}' отсутствует в lead_hunter_prompt.txt"

    def test_sales_prompt_contains_required_fields(self):
        content = _read_prompt("sales")
        for field in ("messages", "qualification_questions", "next_steps",
                      "lead_name", "message_text", "channel"):
            assert field in content, f"Поле '{field}' отсутствует в sales_prompt.txt"

    def test_architect_prompt_complexity_enum(self):
        """Промпт описывает те же enum-значения, что и Pydantic-модель."""
        content = _read_prompt("architect")
        for val in ("low", "medium", "high"):
            assert val in content, f"enum-значение '{val}' отсутствует в architect_prompt.txt"

    def test_developer_prompt_file_type_enum(self):
        content = _read_prompt("developer")
        for val in ("n8n_workflow", "javascript", "config"):
            assert val in content, f"enum-значение '{val}' отсутствует в developer_prompt.txt"

    def test_sales_prompt_channel_enum(self):
        content = _read_prompt("sales")
        for val in ("telegram", "email", "phone"):
            assert val in content, f"channel enum '{val}' отсутствует в sales_prompt.txt"

    def test_tech_writer_prompt_document_type_enum(self):
        """tech_writer_prompt описывает commercial_proposal — должен быть в Document.type."""
        content = _read_prompt("tech_writer")
        assert "commercial_proposal" in content
        from core.schemas import Document
        schema = Document.model_json_schema()
        assert "commercial_proposal" in str(schema)


# ══════════════════════════════════════════════════════════════════
# PMDeadlockResolution — 'need_human_review' в Literal
# ══════════════════════════════════════════════════════════════════

class TestPMDeadlockSolution:
    """pm_prompt.txt описывает 'need_human_review' — Pydantic-модель должна принимать его."""

    def test_need_human_review_accepted(self):
        from core.schemas import PMDeadlockResolution
        obj = PMDeadlockResolution(
            analysis="Задача застряла",
            solution="need_human_review",
            actions=[{"action": "update_task", "task_id": "task_001",
                      "new_status": "needs_human_review"}],
            comment="Требуется человек",
        )
        assert obj.solution == "need_human_review"

    def test_all_solutions_are_valid(self):
        from core.schemas import PMDeadlockResolution
        from pydantic import ValidationError
        valid_solutions = [
            "update_dependencies", "skip_tasks", "create_tasks",
            "stop_project", "need_human_review",
        ]
        for sol in valid_solutions:
            obj = PMDeadlockResolution(
                analysis="test", solution=sol, actions=[], comment="ok"
            )
            assert obj.solution == sol

    def test_invalid_solution_raises(self):
        from core.schemas import PMDeadlockResolution
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            PMDeadlockResolution(
                analysis="test", solution="unknown_action", actions=[], comment="fail"
            )

    def test_pm_prompt_contains_need_human_review(self):
        """pm_prompt.txt упоминает 'need_human_review' — должно совпадать с Literal."""
        content = _read_prompt("pm")
        assert "need_human_review" in content


# ══════════════════════════════════════════════════════════════════
# AnalystResponse.handoff_to_architect
# ══════════════════════════════════════════════════════════════════

class TestAnalystHandoff:
    """handoff_to_architect сохраняется в AnalystResponse и передаётся архитектору."""

    def test_handoff_to_architect_accepted(self):
        from core.schemas import AnalystResponse
        data = {
            "client_name": "ООО Тест",
            "current_pain_points": [],
            "proposed_automation": [],
            "roi_calculation": {
                "total_time_saved_hours_per_month": 30.0,
                "cost_saved_per_month_rub": 30000.0,
                "implementation_cost_rub": 50000.0,
                "payback_period_months": 1.7,
            },
            "proposal_structure": ["Слайд 1"],
            "handoff_to_architect": {
                "required_integrations": ["WB API", "n8n"],
                "data_volume_estimate": "1000 заявок/день",
                "priority_automations": ["Обработка отзывов"],
                "constraints": ["Бюджет 50к", "Срок 2 недели"],
            },
        }
        obj = AnalystResponse(**data)
        assert obj.handoff_to_architect is not None
        assert "required_integrations" in obj.handoff_to_architect
        assert obj.handoff_to_architect["constraints"] == ["Бюджет 50к", "Срок 2 недели"]

    def test_handoff_to_architect_optional(self):
        """Если LLM не сгенерировал handoff — модель валидируется без него."""
        from core.schemas import AnalystResponse
        obj = AnalystResponse(
            client_name="ООО Тест",
            proposal_structure=["Слайд 1"],
        )
        assert obj.handoff_to_architect is None

    def test_handle_analyst_stores_handoff_in_context(self):
        """handle_analyst должен сохранять handoff_to_architect в analyst_context."""
        from core.schemas import AnalystResponse, ROICalculation, PainPoint
        from core.agent_handlers import AgentHandlers

        # Собираем mock orchestrator
        mock_orch = MagicMock()
        mock_orch.current_project = {"Id": 1, "tokens_used": 0}
        mock_orch.tasks_db = MagicMock()
        mock_orch.projects_db = MagicMock()

        handlers = AgentHandlers(orchestrator=mock_orch, qa_gate=MagicMock())

        analyst_response = AnalystResponse(
            client_name="ООО Тест",
            current_pain_points=[
                PainPoint(process="Ручная обработка", time_per_day_hours=2.0, cost_per_month_rub=20000.0)
            ],
            proposed_automation=[],
            roi_calculation=ROICalculation(
                total_time_saved_hours_per_month=30.0,
                cost_saved_per_month_rub=30000.0,
                implementation_cost_rub=50000.0,
                payback_period_months=1.7,
            ),
            proposal_structure=["Слайд 1"],
            handoff_to_architect={
                "required_integrations": ["WB API"],
                "constraints": ["Бюджет 50к"],
            },
        )

        result = handlers.handle_analyst(
            task={"Id": 10, "task_id": "task_001"},
            task_db_id=10,
            task_name="task_001",
            agent_response=analyst_response,
            pm_prompt="",
        )

        assert result is True
        ctx = mock_orch.current_project["analyst_context"]
        assert "handoff_to_architect" in ctx
        assert ctx["handoff_to_architect"]["required_integrations"] == ["WB API"]

    def test_handle_analyst_without_handoff(self):
        """handle_analyst без handoff не ломается и не добавляет ключ."""
        from core.schemas import AnalystResponse
        from core.agent_handlers import AgentHandlers

        mock_orch = MagicMock()
        mock_orch.current_project = {"Id": 1, "tokens_used": 0}
        mock_orch.tasks_db = MagicMock()
        mock_orch.projects_db = MagicMock()

        handlers = AgentHandlers(orchestrator=mock_orch, qa_gate=MagicMock())
        response = AnalystResponse(client_name="X", proposal_structure=[])

        handlers.handle_analyst(
            task={"Id": 5, "task_id": "t1"},
            task_db_id=5,
            task_name="t1",
            agent_response=response,
            pm_prompt="",
        )

        ctx = mock_orch.current_project["analyst_context"]
        assert "handoff_to_architect" not in ctx


# ══════════════════════════════════════════════════════════════════
# LeadHunterResponse.handoff_to_sales
# ══════════════════════════════════════════════════════════════════

class TestLeadHunterHandoff:
    """handoff_to_sales сохраняется в LeadHunterResponse и передаётся sales-агенту."""

    def test_handoff_to_sales_accepted(self):
        from core.schemas import LeadHunterResponse, Lead
        obj = LeadHunterResponse(
            leads_found=[
                Lead(
                    company_name="ООО Ромашка",
                    marketplace="WB",
                    category="Одежда",
                    pain_points=["Много отзывов"],
                    source="Telegram",
                )
            ],
            total_found=1,
            handoff_to_sales={
                "recommended_approach": "Тёплый тон",
                "key_pain_points": ["Много отзывов"],
                "best_contacts": ["telegram"],
            },
        )
        assert obj.handoff_to_sales is not None
        assert obj.handoff_to_sales["recommended_approach"] == "Тёплый тон"

    def test_handoff_to_sales_optional(self):
        from core.schemas import LeadHunterResponse
        obj = LeadHunterResponse(leads_found=[], total_found=0)
        assert obj.handoff_to_sales is None

    def test_handle_lead_hunter_stores_handoff(self):
        """handle_lead_hunter должен сохранять handoff_to_sales в leads_handoff_to_sales."""
        from core.schemas import LeadHunterResponse, Lead
        from core.agent_handlers import AgentHandlers

        mock_orch = MagicMock()
        mock_orch.current_project = {"Id": 1, "tokens_used": 0}
        mock_orch.tasks_db = MagicMock()

        handlers = AgentHandlers(orchestrator=mock_orch, qa_gate=MagicMock())

        lead_response = LeadHunterResponse(
            leads_found=[
                Lead(
                    company_name="ООО Ромашка",
                    marketplace="WB",
                    category="Одежда",
                    pain_points=["Ручная обработка"],
                    source="Telegram",
                )
            ],
            total_found=1,
            handoff_to_sales={"recommended_approach": "Тёплый", "key_pain_points": ["боль"]},
        )

        result = handlers.handle_lead_hunter(
            task={"Id": 1, "task_id": "t1", "task_description": "Поиск лидов"},
            task_db_id=1,
            task_name="t1",
            agent_response=lead_response,
            pm_prompt="",
        )

        assert result is True
        assert "leads_handoff_to_sales" in mock_orch.current_project
        assert mock_orch.current_project["leads_handoff_to_sales"]["recommended_approach"] == "Тёплый"

    def test_handle_lead_hunter_without_handoff(self):
        """handle_lead_hunter без handoff не ломается."""
        from core.schemas import LeadHunterResponse, Lead
        from core.agent_handlers import AgentHandlers

        mock_orch = MagicMock()
        mock_orch.current_project = {"Id": 1, "tokens_used": 0}
        mock_orch.tasks_db = MagicMock()

        handlers = AgentHandlers(orchestrator=mock_orch, qa_gate=MagicMock())
        lead_response = LeadHunterResponse(leads_found=[], total_found=0)

        handlers.handle_lead_hunter(
            task={"Id": 1, "task_id": "t1", "task_description": ""},
            task_db_id=1,
            task_name="t1",
            agent_response=lead_response,
            pm_prompt="",
        )

        assert "leads_handoff_to_sales" not in mock_orch.current_project


# ══════════════════════════════════════════════════════════════════
# QA Gate vs Спец-хендлер: диспатч в TaskExecutor
# ══════════════════════════════════════════════════════════════════

class TestAgentDispatch:
    """Проверяет, что правильные агенты получают спец-хендлеры, а не QA Gate."""

    SPEC_HANDLER_AGENTS = {"architect", "lead_hunter", "sales", "analyst"}
    QA_GATE_AGENTS = {"developer", "crm_customizer", "tech_writer"}

    def test_spec_handler_agents_are_documented(self):
        """Спец-хендлеры покрывают ожидаемый набор агентов."""
        from core.task_executor import TaskExecutor
        import inspect
        source = inspect.getsource(TaskExecutor.execute)
        for agent in self.SPEC_HANDLER_AGENTS:
            assert f'agent_name == "{agent}"' in source, (
                f"Агент '{agent}' должен диспатчиться через спец-хендлер в TaskExecutor.execute"
            )

    def test_qa_gate_called_for_standard_agents(self):
        """Стандартные агенты идут через run_qa_gate, а не через спец-хендлеры."""
        from core.task_executor import TaskExecutor
        import inspect
        source = inspect.getsource(TaskExecutor.execute)
        # Стандартный путь: вызов run_qa_gate существует
        assert "run_qa_gate" in source

    def test_qa_agent_itself_skips_qa_gate(self):
        """qa-агент не должен сам проходить QA Gate — это бесконечная рекурсия."""
        from core.task_executor import TaskExecutor
        import inspect
        source = inspect.getsource(TaskExecutor.execute)
        # Ветка `if agent_name != "qa":` перед run_qa_gate
        assert 'agent_name != "qa"' in source
