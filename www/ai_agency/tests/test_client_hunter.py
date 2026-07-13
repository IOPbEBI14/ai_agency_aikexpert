"""Тесты агента client_hunter (монетизация: Google + УТП)."""
from unittest.mock import MagicMock, patch

import pytest

from core.client_hunter_tools import (
    build_search_queries,
    company_name_from_title,
    run_google_only_search,
)
from core.schemas import ClientHunterResponse, ClientProspect, ClientUSP


class TestClientHunterTools:
    def test_build_queries_from_goal(self):
        qs = build_search_queries(goal="Автоматизация для селлеров WB", max_queries=3)
        assert qs
        assert any("wildberries" in q.lower() or "селлер" in q.lower() for q in qs)

    def test_company_name_from_title(self):
        assert company_name_from_title("ТехноФикс | Официальный сайт") == "ТехноФикс"

    @patch("core.client_hunter_tools.lead_tools")
    @patch("core.client_hunter_tools.Config")
    def test_google_only_dedupes(self, mock_cfg, mock_tools):
        mock_cfg.GOOGLE_API_KEY = "key"
        mock_cfg.GOOGLE_CX = "cx"
        mock_tools.search_google.side_effect = [
            [
                {"title": "A", "link": "https://a.example", "snippet": "s1"},
                {"title": "B", "link": "https://b.example", "snippet": "s2"},
            ],
            [
                {"title": "A again", "link": "https://a.example", "snippet": "dup"},
            ],
        ]
        results = run_google_only_search(["q1", "q2"], results_per_query=5)
        assert len(results) == 2
        assert all(r["source"] == "google" for r in results)

    @patch("core.client_hunter_tools.Config")
    def test_google_missing_keys(self, mock_cfg):
        mock_cfg.GOOGLE_API_KEY = ""
        mock_cfg.GOOGLE_CX = ""
        assert run_google_only_search(["q"]) == []


class TestClientHunterSchema:
    def test_valid_response(self):
        obj = ClientHunterResponse(
            summary="ok",
            search_queries=["q1"],
            clients=[
                ClientProspect(
                    company_name="X",
                    website="https://x.test",
                    snippet="snip",
                    niche="ecom",
                    pain_hypothesis=["ручные заявки"],
                    usp=ClientUSP(
                        headline="H",
                        value_proposition="V",
                        differentiators=["d1"],
                        call_to_action="демо",
                    ),
                    source="google",
                    source_query="q1",
                )
            ],
            total_found=1,
        )
        assert obj.clients[0].source == "google"
        assert obj.total_found == 1

    def test_agent_models_has_client_hunter(self):
        from core.schemas import AGENT_MODELS

        assert "client_hunter" in AGENT_MODELS
        assert AGENT_MODELS["client_hunter"] is ClientHunterResponse


class TestHandleClientHunter:
    @patch("core.agent_handlers.log_to_agent_logs")
    def test_stores_context(self, mock_log, orchestrator, mock_nocodb_clients, sample_project_data):
        orchestrator.current_project = sample_project_data
        response = ClientHunterResponse(
            summary="Найден 1 клиент",
            search_queries=["селлер автоматизация"],
            clients=[
                ClientProspect(
                    company_name="ТехноФикс",
                    website="https://technofix.test",
                    snippet="Магазин",
                    niche="автозапчасти",
                    pain_hypothesis=["ручные заявки"],
                    usp=ClientUSP(
                        headline="Заявки в CRM за день",
                        value_proposition="Автосбор из мессенджеров",
                        differentiators=["blueprint под стек"],
                        call_to_action="15-мин демо",
                    ),
                    source="google",
                    source_query="селлер автоматизация",
                )
            ],
            total_found=1,
            handoff_to_sales={"recommended_approach": "короткое УТП"},
        )
        ok = orchestrator._handle_client_hunter(
            {"Id": 1, "task_id": "task_ch"},
            1,
            "task_ch",
            response,
            "pm",
        )
        assert ok is True
        assert len(orchestrator.current_project["client_hunter_context"]) == 1
        assert (
            orchestrator.current_project["client_hunter_context"][0]["usp"]["headline"]
            == "Заявки в CRM за день"
        )
        assert "leads_context" in orchestrator.current_project
        assert orchestrator.current_project["client_hunter_handoff_to_sales"][
            "recommended_approach"
        ] == "короткое УТП"
