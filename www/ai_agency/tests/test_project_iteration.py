"""Тесты итераций проекта (Direction X)."""
from unittest.mock import MagicMock, patch

import pytest

from core.project_iteration import (
    build_previous_tasks_digest,
    enrich_task_input,
    get_project_iteration,
    normalize_iteration_task_ids,
    prepare_iteration_metrics,
)


class TestProjectIterationHelpers:
    def test_get_iteration_from_metrics(self):
        assert get_project_iteration({"metrics": '{"iteration": 3}'}) == 3
        assert get_project_iteration({"iteration": 2}) == 2
        assert get_project_iteration({}) == 1

    def test_prepare_archives_report(self):
        project = {
            "metrics": '{"iteration": 1}',
            "final_report": "# Отчёт v1\n" + ("x" * 20000),
            "completed_at": "2026-01-01",
        }
        metrics, nxt = prepare_iteration_metrics(
            project, next_iteration=2, human_remarks="Исправь loop"
        )
        assert nxt == 2
        assert metrics["iteration"] == 2
        hist0 = metrics["iteration_history"][0]
        assert hist0["final_report"].startswith("# Отчёт")
        assert len(hist0["final_report"]) <= 8000
        assert hist0["final_report_chars"] == len(project["final_report"])
        assert "loop" in metrics["last_human_remarks"]

    def test_normalize_prefixes_and_external_deps(self):
        tasks = [
            {
                "task_id": "task_001",
                "depends_on": ["old_dev"],
                "input_data": {},
            },
            {
                "task_id": "task_002",
                "depends_on": ["task_001"],
                "input_data": {},
            },
        ]
        out = normalize_iteration_task_ids(tasks, 2)
        ids = {t["task_id"] for t in out}
        assert "iter2_task_001" in ids
        assert "iter2_task_002" in ids
        t1 = next(t for t in out if t["task_id"] == "iter2_task_001")
        assert t1["depends_on"] == []
        assert "old_dev" in t1["input_data"]["previous_task_ids"]
        t2 = next(t for t in out if t["task_id"] == "iter2_task_002")
        assert t2["depends_on"] == ["iter2_task_001"]

    def test_enrich_includes_previous_outputs(self):
        prev = {
            "dev_001": {
                "agent_name": "developer",
                "status": "completed",
                "output_data": '{"n8n_json": {"nodes": []}}',
            }
        }
        payload = enrich_task_input(
            {
                "agent_name": "developer",
                "input_data": {"previous_task_ids": ["dev_001"]},
            },
            project={"goal": "g", "client_name": "c", "project_name": "p"},
            human_remarks="fix retry",
            iteration=2,
            previous_by_id=prev,
        )
        import json
        data = json.loads(payload)
        assert data["project_iteration"] == 2
        assert "dev_001" in data["previous_iteration_outputs"]
        assert "fix retry" in data["human_remarks"]

    def test_digest_limits(self):
        tasks = [
            {"task_id": f"t{i}", "agent_name": "qa", "status": "completed",
             "task_description": "d", "output_data": "x"}
            for i in range(30)
        ]
        assert len(build_previous_tasks_digest(tasks, limit=10)) == 10


class TestRefineApi:
    @pytest.fixture
    def client(self):
        with patch("core.nocodb.NocoDBClient"), \
             patch("core.nocodb.ProjectsClient"), \
             patch("core.nocodb.TasksClient"), \
             patch("core.orchestrator.Orchestrator"):
            import main as app_module
            from fastapi.testclient import TestClient

            app_module.orchestrator = MagicMock()
            app_module.orchestrator.agency_running = False
            app_module.orchestrator.current_project = None
            app_module.agency_task = None
            with TestClient(app_module.app) as c:
                yield c, app_module

    def test_refine_requires_project(self, client):
        c, mod = client
        mod.orchestrator.current_project = None
        resp = c.post(
            "/api/agency/refine",
            json={"human_prompt": "нужно исправить workflow"},
        )
        assert resp.status_code == 400

    def test_refine_success(self, client):
        c, mod = client
        mod.orchestrator.initialize.return_value = True
        mod.orchestrator.current_project = {"Id": 42, "project_name": "P"}
        mod.orchestrator.start_project_iteration.return_value = {
            "iteration": 2,
            "tasks_created": 3,
            "project_id": 42,
            "pm_comment": "Доработка developer+docs",
        }
        with patch.object(mod, "_start_orchestrator_background") as start:
            resp = c.post(
                "/api/agency/refine",
                json={
                    "human_prompt": "Замени цикл на Loop Over Items",
                    "project_id": 42,
                    "resume": True,
                },
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "iteration_started"
        assert data["iteration"] == 2
        assert data["tasks_created"] == 3
        assert data["resumed"] is True
        start.assert_called_once()
        mod.orchestrator.initialize.assert_called_with(42)
