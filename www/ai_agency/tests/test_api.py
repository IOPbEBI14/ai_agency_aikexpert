"""Тесты FastAPI HTTP-слоя (контракт API + валидация)."""
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    with patch("core.nocodb.NocoDBClient"), \
         patch("core.nocodb.ProjectsClient"), \
         patch("core.nocodb.TasksClient"), \
         patch("core.orchestrator.Orchestrator"):
        import main as app_module
        app_module.orchestrator = MagicMock()
        app_module.orchestrator.agency_running = False
        app_module.orchestrator.current_project = None
        app_module.agency_task = None
        with TestClient(app_module.app) as c:
            yield c, app_module


class TestAgencyApiContract:
    def test_status_idle(self, client):
        c, mod = client
        # Явно: ни один статус не резюмируем (см. TestResumableProjectStatus
        # для проверки самого механизма поиска резюмируемого проекта).
        mod.projects_db.find_project_by_status.return_value = None
        resp = c.get("/api/agency/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "idle"
        assert "tasks" in data
        assert "pm" in data
        assert data["running"] is False

    def test_stop(self, client):
        c, mod = client
        resp = c.post("/api/agency/stop")
        assert resp.status_code == 200
        assert resp.json()["status"] == "stopped"
        mod.orchestrator.stop.assert_called()

    def test_start_already_running(self, client):
        c, mod = client
        mod.orchestrator.agency_running = True
        resp = c.post("/api/agency/start")
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_human_review_validation_422(self, client):
        c, _ = client
        resp = c.post("/api/agency/human-review", json={})
        assert resp.status_code == 422

    def test_human_review_empty_prompt_422(self, client):
        c, _ = client
        resp = c.post("/api/agency/human-review", json={"human_prompt": "   "})
        assert resp.status_code == 422

    def test_human_review_no_project(self, client):
        c, mod = client
        mod.orchestrator.current_project = None
        resp = c.post(
            "/api/agency/human-review",
            json={"human_prompt": "исправь задачу"},
        )
        assert resp.status_code == 400
        assert "error" in resp.json()

    def test_projects_limit_validation(self, client):
        c, _ = client
        resp = c.get("/api/agency/projects?limit=0")
        assert resp.status_code == 422
        resp = c.get("/api/agency/projects?limit=999")
        assert resp.status_code == 422

    def test_openapi_docs(self, client):
        c, _ = client
        resp = c.get("/docs")
        assert resp.status_code == 200
        resp = c.get("/openapi.json")
        assert resp.status_code == 200
        schema = resp.json()
        assert "/api/agency/status" in schema["paths"]
        assert "/api/agency/start" in schema["paths"]

    def test_start_success(self, client):
        c, mod = client
        mod.orchestrator.agency_running = False
        mod.orchestrator.initialize.return_value = True
        mod.orchestrator.current_project = {
            "project_name": "Test",
            "current_phase": "analysis",
            "tokens_used": 0,
            "token_budget": 50000,
            "Id": 1,
        }
        with patch.object(mod, "_start_orchestrator_background"):
            resp = c.post("/api/agency/start")
        assert resp.status_code == 200
        assert resp.json()["status"] == "started"

    def test_start_create_project_422(self, client):
        c, _ = client
        resp = c.post("/api/agency/start", json={"project_name": "X"})
        assert resp.status_code == 422

    def test_start_create_project_goal_too_short(self, client):
        c, _ = client
        resp = c.post(
            "/api/agency/start",
            json={
                "project_name": "Proj",
                "client_name": "Client",
                "goal": "short",
            },
        )
        assert resp.status_code == 422

    def test_start_create_project_success(self, client):
        c, mod = client
        mod.orchestrator.agency_running = False
        mod.orchestrator.initialize.return_value = True
        mod.orchestrator.current_project = {
            "project_name": "Автоматизация ТехноФикс",
            "client_name": "ООО ТехноФикс",
            "current_phase": "lead_gen",
            "tokens_used": 0,
            "token_budget": 40000,
            "Id": 42,
        }
        payload = {
            "project_name": "Автоматизация ТехноФикс",
            "client_name": "ООО ТехноФикс",
            "goal": "Найти клиентов и предложить автоматизацию заявок из Telegram",
            "token_budget": 40000,
            "current_phase": "lead_gen",
        }
        with patch.object(mod, "_start_orchestrator_background"):
            resp = c.post("/api/agency/start", json=payload)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "started"
        assert data["project_id"] == 42
        mod.orchestrator.initialize.assert_called_once()
        call_kw = mod.orchestrator.initialize.call_args.kwargs
        assert call_kw["create"]["project_name"] == payload["project_name"]
        assert call_kw["create"]["goal"] == payload["goal"]
        assert call_kw["create"]["token_budget"] == 40000


class TestResumableProjectStatus:
    """Замечание: проект в БД со статусом stopped/needs_human_review должен
    быть виден в /api/agency/status (и, следовательно, доступен для
    «Продолжить»), даже если orchestrator.current_project не загружен
    в память (например, после перезапуска процесса)."""

    def _project(self, **overrides):
        base = {
            "Id": 7,
            "project_name": "Автоматизация ТехноФикс",
            "client_name": "ООО ТехноФикс",
            "status": "stopped",
            "current_phase": "development",
            "tokens_used": 1000,
            "token_budget": 30000,
            "goal": "Автоматизировать обработку заявок",
            "final_report": "",
            "metrics": "{}",
            "completed_at": "",
        }
        base.update(overrides)
        return base

    def test_status_reflects_stopped_project_without_memory(self, client):
        c, mod = client
        assert mod.orchestrator.current_project is None

        def side_effect(status):
            return self._project(status="stopped") if status == "stopped" else None

        mod.projects_db.find_project_by_status.side_effect = side_effect
        mod.tasks_db.get_tasks_by_project.return_value = []

        resp = c.get("/api/agency/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "stopped"
        assert data["project_id"] == 7
        assert data["running"] is False

    def test_status_reflects_needs_human_review_without_memory(self, client):
        c, mod = client
        assert mod.orchestrator.current_project is None

        def side_effect(status):
            return self._project(status="needs_human_review", Id=9) if status == "needs_human_review" else None

        mod.projects_db.find_project_by_status.side_effect = side_effect
        mod.tasks_db.get_tasks_by_project.return_value = []

        resp = c.get("/api/agency/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "needs_human_review"
        assert data["project_id"] == 9

    def test_status_prioritizes_in_progress_over_stopped(self, client):
        c, mod = client

        def side_effect(status):
            if status == "in_progress":
                return self._project(status="in_progress", Id=11)
            if status == "stopped":
                return self._project(status="stopped", Id=7)
            return None

        mod.projects_db.find_project_by_status.side_effect = side_effect
        mod.tasks_db.get_tasks_by_project.return_value = []

        resp = c.get("/api/agency/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["project_id"] == 11
        assert data["status"] == "in_progress"

    def test_status_idle_when_no_project_in_db(self, client):
        c, mod = client
        # Мок общий на модуль main (кэш sys.modules) — явно сбрасываем side_effect
        # от предыдущих тестов класса перед заданием return_value.
        mod.projects_db.find_project_by_status.side_effect = None
        mod.projects_db.find_project_by_status.return_value = None
        resp = c.get("/api/agency/status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "idle"

    def test_status_survives_nocodb_errors(self, client):
        """Ошибка NocoDB при поиске резюмируемого проекта не должна валить /status."""
        c, mod = client
        mod.projects_db.find_project_by_status.return_value = None
        mod.projects_db.find_project_by_status.side_effect = RuntimeError("NocoDB недоступен")
        resp = c.get("/api/agency/status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "idle"

    def test_status_ignores_non_dict_mock_leakage(self, client):
        """Защита от MagicMock, просочившегося вместо dict/None (см. isinstance-проверку)."""
        c, mod = client
        mod.projects_db.find_project_by_status.side_effect = None
        mod.projects_db.find_project_by_status.return_value = MagicMock()
        resp = c.get("/api/agency/status")
        assert resp.status_code == 200
        assert resp.json()["status"] == "idle"
