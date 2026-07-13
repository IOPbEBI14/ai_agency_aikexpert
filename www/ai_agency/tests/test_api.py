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
        c, _ = client
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
