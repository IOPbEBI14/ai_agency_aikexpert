"""Тесты Фазы 3.1: выбор волны и параллельный запуск ready-задач."""
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from core.config import Config
from core.orchestrator import Orchestrator
from core.parallel_wave import select_parallel_wave


def _task(tid: str, agent: str, depends_on=None):
    return {
        "Id": tid,
        "task_id": tid,
        "agent_name": agent,
        "status": "pending",
        "depends_on": depends_on if depends_on is not None else [],
    }


class TestSelectParallelWave:
    def test_empty(self):
        assert select_parallel_wave([], 3) == []

    def test_limit_one_returns_first(self):
        ready = [_task("a", "developer"), _task("b", "qa")]
        assert select_parallel_wave(ready, 1) == [ready[0]]

    def test_picks_independent_up_to_limit(self):
        ready = [
            _task("d1", "developer"),
            _task("d2", "developer"),
            _task("d3", "developer"),
            _task("qa", "qa"),
        ]
        wave = select_parallel_wave(ready, 3)
        assert len(wave) == 3
        assert [t["task_id"] for t in wave] == ["d1", "d2", "d3"]

    def test_singleton_agents_not_duplicated(self):
        ready = [
            _task("s1", "sales"),
            _task("s2", "sales"),
            _task("tw", "tech_writer"),
        ]
        wave = select_parallel_wave(ready, 3)
        assert [t["task_id"] for t in wave] == ["s1", "tw"]

    def test_only_one_hunter_in_wave(self):
        ready = [
            _task("ch", "client_hunter"),
            _task("lh", "lead_hunter"),
            _task("dev", "developer"),
        ]
        wave = select_parallel_wave(ready, 3)
        agents = {t["agent_name"] for t in wave}
        assert "client_hunter" in agents
        assert "lead_hunter" not in agents
        assert "developer" in agents

    def test_architect_runs_alone(self):
        ready = [
            _task("arch", "architect"),
            _task("dev", "developer"),
        ]
        assert select_parallel_wave(ready, 3) == [ready[0]]

    def test_architect_skipped_if_wave_started(self):
        ready = [
            _task("dev", "developer"),
            _task("arch", "architect"),
            _task("qa", "qa"),
        ]
        wave = select_parallel_wave(ready, 3)
        assert [t["task_id"] for t in wave] == ["dev", "qa"]


class TestExecuteReadyWave:
    @patch.object(Orchestrator, "execute_task")
    def test_sequential_when_limit_one(self, mock_exec, orchestrator):
        orch = orchestrator
        orch.current_project = {"Id": 1, "tokens_used": 0}
        with patch.object(Config, "MAX_PARALLEL_TASKS", 1):
            orch._execute_ready_wave(
                [_task("a", "developer"), _task("b", "qa")],
                "pm",
                all_tasks=[],
            )
        mock_exec.assert_called_once()
        assert mock_exec.call_args[0][0]["task_id"] == "a"

    @patch.object(Orchestrator, "execute_task")
    def test_parallel_runs_multiple(self, mock_exec, orchestrator):
        orch = orchestrator
        orch.current_project = {"Id": 1, "tokens_used": 0}
        mock_exec.return_value = True
        ready = [
            _task("d1", "developer"),
            _task("d2", "developer"),
            _task("qa", "qa"),
        ]
        with patch.object(Config, "MAX_PARALLEL_TASKS", 3):
            orch._execute_ready_wave(ready, "pm", all_tasks=ready)

        assert mock_exec.call_count == 3
        called_ids = {c.args[0]["task_id"] for c in mock_exec.call_args_list}
        assert called_ids == {"d1", "d2", "qa"}

    def test_add_tokens_thread_safe(self, orchestrator, mock_nocodb_clients):
        orch = orchestrator
        orch.current_project = {"Id": 42, "tokens_used": 0}

        def bump(_):
            orch.add_tokens(10)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(bump, range(20)))

        assert orch.current_project["tokens_used"] == 200
        assert mock_nocodb_clients["projects"].update_project.call_count == 20


class TestOrchestratorRunParallel:
    @patch.object(Orchestrator, "execute_task")
    def test_run_executes_wave(
        self, mock_execute, orchestrator, mock_nocodb_clients, sample_project_data
    ):
        orchestrator.current_project = sample_project_data
        mock_nocodb_clients["tasks"].get_tasks_by_project.return_value = [
            {"task_id": "task_001", "agent_name": "developer", "status": "pending", "depends_on": "[]"},
            {"task_id": "task_002", "agent_name": "developer", "status": "pending", "depends_on": "[]"},
            {"task_id": "task_003", "agent_name": "qa", "status": "pending", "depends_on": '["task_001"]'},
        ]
        mock_execute.return_value = True
        orchestrator.MAX_TOTAL_ITERATIONS = 1
        with patch.object(Config, "MAX_PARALLEL_TASKS", 3):
            orchestrator.run()

        # На 1-й итерации цикла готовы task_001 и task_002 (qa ждёт)
        called = {c.args[0]["task_id"] for c in mock_execute.call_args_list}
        assert called == {"task_001", "task_002"}
