"""Direction AL: декомпозиция tech_writer + merge отчёта в родителе."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from core.task_ids import (
    is_tech_writer_placeholder_task,
    is_tech_writer_subtask_id,
    iteration_prefix_from_task_id,
)
from core.tech_writer_decomposition import (
    build_tech_writer_subtasks,
    merge_tech_writer_slices,
    validate_tech_writer_slice,
)


def _sec(title: str, content: str) -> dict:
    return {
        "title": title,
        "content": content,
        "screenshot_needed": False,
        "screenshot_description": None,
    }


def _overview_out() -> dict:
    return {
        "summary": "Обзор интеграции готов полностью",
        "documents": [{
            "title": "Документация интеграции",
            "type": "integration_guide",
            "audience": "Админ",
            "sections": [
                _sec("Цель интеграции", "Сценарий передаёт события во внешний API."),
                _sec("Источник данных и получатель", "Источник webhook, получатель HTTP API."),
                _sec("Версия API", "Работаем с API v1 получателя данных."),
                _sec("Способ получения событий (webhook)", "Webhook выбран для near-realtime."),
                _sec("Лимиты и постраничная выдача", "Rate limit учтён; пагинация не нужна."),
            ],
        }],
        "video_scripts": [],
        "faq": [],
        "checklist": [],
        "notes": None,
    }


def _contract_out() -> dict:
    return {
        "summary": "Контракт и ошибки описаны детально",
        "documents": [{
            "title": "Документация интеграции",
            "type": "integration_guide",
            "audience": "Админ",
            "sections": [
                _sec("Критичные поля", "event_id обязателен, имя поля менять нельзя."),
                _sec("Контракт данных", "JSON с event_id и payload; без event_id — стоп."),
                _sec("Обработка ошибок", "503 — retry; 401 — permanent + алерт в Telegram."),
                _sec("Адаптер / нормализация", "Set-нода нормализует payload до контракта."),
            ],
        }],
        "video_scripts": [],
        "faq": [],
        "checklist": [],
    }


def _ops_out() -> dict:
    return {
        "summary": "Тесты, FAQ и чек-лист приёмки готовы",
        "documents": [{
            "title": "Документация интеграции",
            "type": "integration_guide",
            "audience": "Админ",
            "sections": [
                _sec("Тестирование", "Проверить успех, 503 timeout и дубль event_id."),
                _sec("Сопровождение", "Ответственный — команда интеграции клиента."),
            ],
        }],
        "video_scripts": [],
        "faq": [
            {"question": "Что при 401?", "answer": "Проверить токен и журнал ошибок."},
            {"question": "Где логи?", "answer": "В n8n execution log и таблице журнала."},
            {"question": "Как повторить 503?", "answer": "Подменить URL на stub с 503."},
        ],
        "checklist": [
            "Контракт данных",
            "Критичные поля",
            "Обработка ошибок",
            "Дедуп event_id",
            "Алерт при 401",
        ],
    }


class TestTechWriterIds:
    def test_ids(self):
        assert is_tech_writer_subtask_id("tw_001")
        assert is_tech_writer_subtask_id("iter3_tw_002")
        assert not is_tech_writer_subtask_id("task_005")
        assert is_tech_writer_placeholder_task(
            {"task_id": "task_005", "agent_name": "tech_writer"}
        )
        assert is_tech_writer_placeholder_task(
            {"task_id": "iter2_task_005", "agent_name": "tech_writer"}
        )
        assert not is_tech_writer_placeholder_task(
            {"task_id": "tw_001", "agent_name": "tech_writer"}
        )
        assert iteration_prefix_from_task_id("iter3_task_005") == "iter3_"
        assert iteration_prefix_from_task_id("task_005") == ""


class TestBuildSubtasks:
    def test_builds_three_slices_with_iter_prefix(self):
        subs = build_tech_writer_subtasks(
            {"task_id": "iter2_task_005", "agent_name": "tech_writer"},
            parent_input={"dependency_outputs": {"task_003": {}}},
        )
        assert [s["task_id"] for s in subs] == [
            "iter2_tw_001",
            "iter2_tw_002",
            "iter2_tw_003",
        ]
        assert [s["doc_slice"] for s in subs] == ["overview", "contract", "ops"]
        assert subs[0]["input_data"]["doc_slice"] == "overview"
        assert "dependency_outputs" in subs[0]["input_data"]


class TestSliceValidation:
    def test_overview_ok(self):
        ok, issues = validate_tech_writer_slice(_overview_out(), "overview")
        assert ok, issues

    def test_overview_missing_theme(self):
        bad = _overview_out()
        bad["documents"][0]["sections"] = bad["documents"][0]["sections"][:2]
        ok, issues = validate_tech_writer_slice(bad, "overview")
        assert not ok
        assert issues


class TestMerge:
    def test_merge_passes_full_schema(self):
        merged = merge_tech_writer_slices([
            _overview_out(),
            _contract_out(),
            _ops_out(),
        ])
        assert merged["documents"][0]["type"] == "integration_guide"
        assert len(merged["documents"][0]["sections"]) >= 8
        assert len(merged["faq"]) >= 3
        assert len(merged["checklist"]) >= 5
        assert merged["notes"] == "merged_from_tech_writer_subtasks"


class TestOrchestratorMergeParent:
    def test_completes_parent_with_merged_output(self, mock_nocodb_clients):
        from core.orchestrator import Orchestrator

        orch = Orchestrator()
        orch.tasks_db = mock_nocodb_clients["tasks"]
        orch.current_project = {"Id": 9}

        tasks = [
            {
                "Id": 50,
                "task_id": "task_005",
                "agent_name": "tech_writer",
                "status": "failed",
            },
            {
                "Id": 51,
                "task_id": "tw_001",
                "agent_name": "tech_writer",
                "status": "completed",
                "output_data": json.dumps(_overview_out(), ensure_ascii=False),
            },
            {
                "Id": 52,
                "task_id": "tw_002",
                "agent_name": "tech_writer",
                "status": "completed",
                "output_data": json.dumps(_contract_out(), ensure_ascii=False),
            },
            {
                "Id": 53,
                "task_id": "tw_003",
                "agent_name": "tech_writer",
                "status": "completed",
                "output_data": json.dumps(_ops_out(), ensure_ascii=False),
            },
        ]
        with patch("core.orchestrator.log_to_agent_logs"):
            orch.check_and_complete_parent_tasks(tasks)

        mock_nocodb_clients["tasks"].update_task.assert_called()
        args = mock_nocodb_clients["tasks"].update_task.call_args
        assert args[0][0] == 50
        assert args[0][1]["status"] == "completed"
        out = json.loads(args[0][1]["output_data"])
        assert out["documents"][0]["type"] == "integration_guide"
        assert len(out["checklist"]) >= 5


class TestDecomposeOnExecute:
    def test_placeholder_creates_subtasks(self, mock_nocodb_clients):
        from core.task_executor import TaskExecutor

        orch = MagicMock()
        orch.MAX_TASK_ITERATIONS = 3
        orch.current_project = {"Id": 1, "tokens_used": 0, "token_budget": 999999}
        orch.tasks_db = mock_nocodb_clients["tasks"]
        orch.tasks_db.get_tasks_by_project = MagicMock(return_value=[])
        orch._build_enriched_input_data = MagicMock(return_value={"x": 1})

        executor = TaskExecutor(orch)
        task = {
            "Id": 5,
            "task_id": "task_005",
            "agent_name": "tech_writer",
            "task_description": "Документация",
            "input_data": "{}",
            "status": "pending",
            "iteration_count": 0,
            "tokens_used": 0,
        }
        with patch("core.task_executor.load_task_context", return_value={"attempts": []}), patch(
            "core.task_executor.log_to_agent_logs"
        ):
            ok = executor.execute(task, pm_prompt="pm")

        assert ok is True
        assert mock_nocodb_clients["tasks"].create_task.call_count == 3
        created_ids = [
            c[0][0]["task_id"]
            for c in mock_nocodb_clients["tasks"].create_task.call_args_list
        ]
        assert created_ids == ["tw_001", "tw_002", "tw_003"]
        # parent → failed placeholder
        parent_updates = [
            c[0][1]
            for c in mock_nocodb_clients["tasks"].update_task.call_args_list
            if c[0][0] == 5
        ]
        assert any(u.get("status") == "failed" for u in parent_updates)
