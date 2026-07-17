"""Тесты агента client_hunter (монетизация: Google + УТП)."""
from unittest.mock import MagicMock, patch

import pytest

from core.client_hunter_tools import (
    build_refined_queries,
    build_search_queries,
    company_name_from_title,
    detect_icp,
    filter_prospect_hits,
    looks_like_vendor_or_article,
    run_google_only_search,
    run_icp_search,
)
from core.schemas import ClientHunterResponse, ClientProspect, ClientUSP


class TestClientHunterTools:
    def test_build_queries_from_goal(self):
        qs = build_search_queries(goal="Автоматизация для селлеров WB", max_queries=3)
        assert qs
        assert any("wildberries" in q.lower() or "селлер" in q.lower() for q in qs)

    def test_dental_icp_uses_prospect_queries_not_crm(self):
        goal = (
            "Найти частные стоматологии от 3 кресел с маркетологом; "
            "ЛПР — главврач/собственник. Москва."
        )
        icp = detect_icp(goal=goal)
        assert icp["icp_key"] == "dental"
        assert "Москва" in icp["geo"]
        qs = build_search_queries(goal=goal, max_queries=6)
        assert qs
        assert any("стоматолог" in q.lower() for q in qs)
        assert any("сайт" in q.lower() or "записаться" in q.lower() for q in qs)
        # Старые шаблоны «ниша + CRM/автоматизация» не должны доминировать
        assert not any(
            ("автоматизац" in q.lower() or "crm" in q.lower()) and "стоматолог" in q.lower()
            for q in qs
        )

    def test_vendor_noise_filter(self):
        hits = [
            {
                "title": "YClients — CRM для клиник",
                "link": "https://www.yclients.com/",
                "snippet": "Автоматизация записи",
            },
            {
                "title": "Стоматология Улыбка | Официальный сайт",
                "link": "https://ulybka-dental.example/",
                "snippet": "Записаться к врачу в Москве",
            },
        ]
        assert looks_like_vendor_or_article(hits[0]) is True
        assert looks_like_vendor_or_article(hits[1]) is False
        prospects, noise = filter_prospect_hits(hits)
        assert len(prospects) == 1
        assert len(noise) == 1

    def test_refined_queries_add_cities(self):
        qs = build_refined_queries(
            goal="частная стоматология, ЛПР главврач",
            max_queries=4,
        )
        assert qs
        assert any("Москва" in q or "Санкт-Петербург" in q for q in qs)

    def test_company_name_from_title(self):
        assert company_name_from_title("ТехноФикс | Официальный сайт") == "ТехноФикс"

    @patch("core.client_hunter_tools.run_google_only_search")
    def test_run_icp_search_second_pass_on_noise(self, mock_search):
        mock_search.side_effect = [
            [
                {
                    "title": "CRM для клиник",
                    "link": "https://amocrm.ru/kliniki",
                    "snippet": "Автоматизация медицинских центров",
                    "query": "q1",
                    "source": "google",
                }
            ],
            [
                {
                    "title": "Клиника Дент | Запись",
                    "link": "https://dent-clinic.example/",
                    "snippet": "Стоматология в центре города",
                    "query": "q2",
                    "source": "google",
                }
            ],
        ]
        pack = run_icp_search(
            goal="частная стоматология Москва",
            task_description="ICP: стоматологии",
        )
        assert pack["icp_detected"]["icp_key"] == "dental"
        assert mock_search.call_count == 2
        assert any(h.get("hit_class") == "prospect_candidate" for h in pack["google_search_results"])

    @patch("core.client_hunter_tools.lead_tools")
    @patch("core.client_hunter_tools.openserp_client")
    def test_openserp_primary_source_dedupes(self, mock_openserp, mock_tools):
        """OpenSERP — основной источник; Google API не должен вызываться, если OpenSERP отвечает."""
        mock_openserp.search.side_effect = [
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
        mock_tools.search_google.assert_not_called()

    @patch("core.client_hunter_tools.lead_tools")
    @patch("core.client_hunter_tools.openserp_client")
    @patch("core.client_hunter_tools.Config")
    def test_falls_back_to_google_api_when_openserp_empty(self, mock_cfg, mock_openserp, mock_tools):
        """Резервный сценарий (чек-лист устойчивости): OpenSERP пуст → пробуем Google API."""
        mock_cfg.GOOGLE_API_KEY = "key"
        mock_cfg.GOOGLE_CX = "cx"
        mock_cfg.OPENSERP_ENGINE = "google"
        mock_cfg.OPENSERP_BASE_URL = "http://localhost:7000"
        mock_openserp.search.return_value = []
        mock_tools.search_google.return_value = [
            {"title": "C", "link": "https://c.example", "snippet": "s3"},
        ]
        results = run_google_only_search(["q1"], results_per_query=5)
        assert len(results) == 1
        assert results[0]["source"] == "google"
        mock_tools.search_google.assert_called_once()

    @patch("core.client_hunter_tools.lead_tools")
    @patch("core.client_hunter_tools.openserp_client")
    def test_openserp_exception_falls_back_to_google(self, mock_openserp, mock_tools):
        """Исключение из OpenSERP не должно ронять весь поиск — переходим к резерву."""
        mock_openserp.search.side_effect = RuntimeError("connection refused")
        mock_tools.search_google.return_value = [
            {"title": "D", "link": "https://d.example", "snippet": "s4"},
        ]
        with patch("core.client_hunter_tools.Config") as mock_cfg:
            mock_cfg.GOOGLE_API_KEY = "key"
            mock_cfg.GOOGLE_CX = "cx"
            results = run_google_only_search(["q1"], results_per_query=5)
        assert len(results) == 1
        assert results[0]["source"] == "google"

    @patch("core.client_hunter_tools.lead_tools")
    @patch("core.client_hunter_tools.openserp_client")
    @patch("core.client_hunter_tools.Config")
    def test_both_sources_empty_returns_empty_list(self, mock_cfg, mock_openserp, mock_tools):
        """Ни OpenSERP, ни Google API не настроены/не ответили → честный пустой список."""
        mock_cfg.GOOGLE_API_KEY = ""
        mock_cfg.GOOGLE_CX = ""
        mock_cfg.OPENSERP_ENGINE = "google"
        mock_cfg.OPENSERP_BASE_URL = "http://localhost:7000"
        mock_openserp.search.return_value = []
        assert run_google_only_search(["q"]) == []
        mock_tools.search_google.assert_not_called()


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
