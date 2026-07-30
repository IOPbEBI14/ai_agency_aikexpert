"""Тесты идентификаторов developer-сабтасков и завершения placeholder-родителя."""
from unittest.mock import MagicMock

from core.orchestrator import Orchestrator
from core.task_ids import is_developer_placeholder_task, is_developer_subtask_id


class TestTaskIds:
    def test_subtask_ids(self):
        assert is_developer_subtask_id("dev_001")
        assert is_developer_subtask_id("dev_workflow")
        assert is_developer_subtask_id("iter3_dev_001")
        assert is_developer_subtask_id("iter2_dev_workflow")
        assert not is_developer_subtask_id("task_003")
        assert not is_developer_subtask_id("iter3_task_003")
        assert not is_developer_subtask_id("qa_dev_001")

    def test_placeholder(self):
        assert is_developer_placeholder_task(
            {"task_id": "task_003", "agent_name": "developer"}
        )
        assert is_developer_placeholder_task(
            {"task_id": "iter3_task_004", "agent_name": "developer"}
        )
        assert not is_developer_placeholder_task(
            {"task_id": "iter3_dev_001", "agent_name": "developer"}
        )
        assert not is_developer_placeholder_task(
            {"task_id": "task_003", "agent_name": "qa"}
        )


class TestCompleteParentAfterSubtasks:
    def test_completes_placeholder_when_iter_dev_done(self, mock_nocodb_clients):
        orch = Orchestrator()
        orch.tasks_db = mock_nocodb_clients["tasks"]
        tasks = [
            {
                "Id": 10,
                "task_id": "iter3_task_003",
                "agent_name": "developer",
                "status": "failed",
            },
            {
                "Id": 11,
                "task_id": "iter3_dev_001",
                "agent_name": "developer",
                "status": "completed",
            },
            {
                "Id": 12,
                "task_id": "iter3_dev_002",
                "agent_name": "developer",
                "status": "completed",
            },
        ]
        orch.check_and_complete_parent_tasks(tasks)
        mock_nocodb_clients["tasks"].update_task.assert_called_once()
        args = mock_nocodb_clients["tasks"].update_task.call_args
        assert args[0][0] == 10
        assert args[0][1]["status"] == "completed"

    def test_expand_completed_includes_iter_placeholder(self, mock_nocodb_clients):
        orch = Orchestrator()
        tasks = [
            {"task_id": "iter2_task_003", "agent_name": "developer", "status": "failed"},
            {"task_id": "iter2_dev_001", "agent_name": "developer", "status": "completed"},
        ]
        expanded = orch._expand_completed_with_parents(tasks, ["iter2_dev_001"])
        assert "iter2_task_003" in expanded

    def test_no_complete_while_subtask_pending(self, mock_nocodb_clients):
        orch = Orchestrator()
        orch.tasks_db = mock_nocodb_clients["tasks"]
        tasks = [
            {"Id": 1, "task_id": "task_003", "agent_name": "developer", "status": "failed"},
            {"Id": 2, "task_id": "dev_001", "agent_name": "developer", "status": "completed"},
            {"Id": 3, "task_id": "dev_002", "agent_name": "developer", "status": "pending"},
        ]
        orch.check_and_complete_parent_tasks(tasks)
        mock_nocodb_clients["tasks"].update_task.assert_not_called()
