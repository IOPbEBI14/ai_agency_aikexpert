"""Тесты WebSocket push статуса (Фаза 2.1)."""
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def client():
    with patch("core.nocodb.NocoDBClient"), \
         patch("core.nocodb.ProjectsClient"), \
         patch("core.nocodb.TasksClient"), \
         patch("core.orchestrator.Orchestrator"):
        import main as app_module
        from core import agency_ws

        app_module.orchestrator = MagicMock()
        app_module.orchestrator.agency_running = False
        app_module.orchestrator.current_project = None
        app_module.agency_task = None
        agency_ws.status_hub._clients.clear()
        agency_ws.set_status_builder(app_module.build_agency_status)

        try:
            from fastapi.testclient import TestClient
        except ModuleNotFoundError:
            pytest.skip("fastapi не установлен")

        with TestClient(app_module.app) as c:
            yield c, app_module

        agency_ws.status_hub._clients.clear()


class TestAgencyStatusWebSocket:
    def test_ws_sends_status_snapshot(self, client):
        c, mod = client
        mod.projects_db.find_project_by_status.return_value = None
        with c.websocket_connect("/api/agency/ws") as ws:
            msg = ws.receive_json()
            assert msg["type"] == "status"
            assert "data" in msg
            assert msg["data"]["status"] == "idle"
            assert "running" in msg["data"]
            assert "tasks" in msg["data"]

    def test_ws_ping_pong(self, client):
        c, mod = client
        mod.projects_db.find_project_by_status.return_value = None
        with c.websocket_connect("/api/agency/ws") as ws:
            ws.receive_json()  # snapshot
            ws.send_json({"type": "ping"})
            pong = ws.receive_json()
            assert pong["type"] == "pong"

    def test_build_agency_status_matches_http(self, client):
        c, mod = client
        mod.projects_db.find_project_by_status.return_value = None
        http = c.get("/api/agency/status")
        assert http.status_code == 200
        with c.websocket_connect("/api/agency/ws") as ws:
            msg = ws.receive_json()
        assert msg["data"]["status"] == http.json()["status"]
        assert msg["data"]["running"] == http.json()["running"]


class TestStatusHubUnit:
    def test_broadcast_to_mock_client(self):
        import asyncio
        from unittest.mock import AsyncMock
        from core.agency_ws import StatusHub

        hub = StatusHub()
        ws = MagicMock()
        ws.send_text = AsyncMock()
        hub._clients.add(ws)
        sent = asyncio.run(
            hub.broadcast_json({"type": "status", "data": {"running": False}})
        )
        assert sent == 1
        ws.send_text.assert_awaited_once()
        assert "status" in ws.send_text.await_args.args[0]
