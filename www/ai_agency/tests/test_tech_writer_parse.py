"""Direction AK: tech_writer — логирование raw JSON и строгая валидация."""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from core.schemas import (
    Document,
    DocumentSection,
    FAQItem,
    LLMParseError,
    TechWriterResponse,
    call_and_parse_llm,
    extract_json_from_text,
)


def _guide_sections():
    return [
        DocumentSection(
            title="Цель интеграции",
            content="Сценарий принимает событие и передаёт во внешний сервис.",
            screenshot_needed=False,
        ),
        DocumentSection(
            title="Источник данных и получатель",
            content="Источник — webhook; получатель — внешний HTTP API.",
            screenshot_needed=False,
        ),
        DocumentSection(
            title="Версия API",
            content="Работаем с API v1 получателя; при смене версии проверить контракт.",
            screenshot_needed=False,
        ),
        DocumentSection(
            title="Способ получения событий (webhook)",
            content="Выбран webhook: события нужны near-realtime.",
            screenshot_needed=False,
        ),
        DocumentSection(
            title="Лимиты и постраничная выдача",
            content="Rate limit учтён через Wait; пагинация не используется.",
            screenshot_needed=False,
        ),
        DocumentSection(
            title="Критичные поля",
            content="event_id и payload.type обязательны; менять имена нельзя.",
            screenshot_needed=False,
        ),
        DocumentSection(
            title="Контракт данных",
            content="JSON: event_id string, payload object; без event_id — стоп.",
            screenshot_needed=False,
        ),
        DocumentSection(
            title="Обработка ошибок",
            content="503/timeout — retry; 401 — permanent + алерт; журнал при исчерпании.",
            screenshot_needed=False,
        ),
        DocumentSection(
            title="Адаптер / нормализация",
            content="Set/Code нормализует payload до контракта получателя.",
            screenshot_needed=False,
        ),
        DocumentSection(
            title="Тестирование",
            content="Успех, 503, неверный токен, дубль event_id — сценарии приёмки.",
            screenshot_needed=False,
        ),
        DocumentSection(
            title="Сопровождение",
            content="Ответственный — команда интеграции; эскалация в Telegram.",
            screenshot_needed=False,
        ),
    ]


class TestExtractBalancedJson:
    def test_ignores_braces_inside_strings(self):
        text = 'prefix {"a": "x}y", "b": 1} trailing'
        result = extract_json_from_text(text)
        assert json.loads(result) == {"a": "x}y", "b": 1}

    def test_markdown_fence(self):
        text = '```json\n{"ok": true, "n": 2}\n```'
        assert json.loads(extract_json_from_text(text))["ok"] is True


class TestTechWriterStrictValidation:
    def test_rejects_thin_sections(self):
        with pytest.raises(ValidationError, match="короткие"):
            TechWriterResponse(
                summary="Документация интеграции готова",
                documents=[
                    Document(
                        title="Guide",
                        type="integration_guide",
                        audience="Админ",
                        sections=_guide_sections()[:5]
                        + [
                            DocumentSection(
                                title="Критичные поля",
                                content="id",
                                screenshot_needed=False,
                            )
                        ]
                        + _guide_sections()[6:],
                    )
                ],
                video_scripts=[],
                faq=[FAQItem(question="?", answer="!")],
                checklist=["1", "2", "3", "4", "5"],
            )

    def test_rejects_nested_json_in_section_content(self):
        nested = json.dumps(
            {
                "summary": "x",
                "documents": [{"title": "t", "type": "integration_guide", "sections": []}],
            },
            ensure_ascii=False,
        )
        sections = _guide_sections()
        sections[0] = DocumentSection(
            title="Цель интеграции",
            content=nested,
            screenshot_needed=False,
        )
        with pytest.raises(ValidationError, match="вложенный JSON"):
            TechWriterResponse(
                summary="Документация интеграции готова",
                documents=[
                    Document(
                        title="Guide",
                        type="integration_guide",
                        audience="Админ",
                        sections=sections,
                    )
                ],
                video_scripts=[],
                faq=[FAQItem(question="?", answer="!")],
                checklist=["1", "2", "3", "4", "5"],
            )


class TestCallAndParseLogsRaw:
    def test_raises_llm_parse_error_with_raw(self):
        def fake_llm(agent_name, system_prompt, user_task):
            return ('Вот ответ:\n{"summary": "x"}', 10)

        with pytest.raises(LLMParseError) as ei:
            call_and_parse_llm(
                call_llm_func=fake_llm,
                agent_name="tech_writer",
                system_prompt="sys",
                user_task="task",
                response_model=TechWriterResponse,
                max_retries=0,
            )
        err = ei.value
        assert err.agent_name == "tech_writer"
        assert "summary" in err.raw_response
        assert "не соответствует схеме" in str(err).lower() or "Validation" in str(err) or "схем" in str(err)

    def test_rejects_root_array(self):
        def fake_llm(agent_name, system_prompt, user_task):
            return ("[1, 2, 3]", 5)

        with pytest.raises(LLMParseError) as ei:
            call_and_parse_llm(
                call_llm_func=fake_llm,
                agent_name="tech_writer",
                system_prompt="sys",
                user_task="task",
                response_model=TechWriterResponse,
                max_retries=0,
            )
        assert "[1, 2, 3]" in ei.value.raw_response


class TestTaskExecutorLogsParseFail:
    def test_parse_fail_writes_agent_logs_and_output(self):
        from core.task_executor import TaskExecutor

        orch = MagicMock()
        orch.MAX_TASK_ITERATIONS = 3
        orch.current_project = {
            "Id": 42,
            "tokens_used": 0,
            "token_budget": 1_000_000,
        }
        orch.projects_db.update_project = MagicMock()
        orch.tasks_db.update_task = MagicMock()
        orch.tasks_db.get_tasks_by_project = MagicMock(return_value=[])
        orch._build_enriched_input_data = MagicMock(return_value={})

        executor = TaskExecutor(orch)
        task = {
            "Id": 7,
            "task_id": "task_005",
            "agent_name": "tech_writer",
            "task_description": "Документация",
            "input_data": "{}",
            "status": "pending",
            "iteration_count": 0,
            "tokens_used": 0,
        }

        with patch.object(
            executor,
            "_call_with_validation",
            side_effect=LLMParseError(
                "bad schema",
                agent_name="tech_writer",
                raw_response='{"weird": true, "documents": []}',
            ),
        ), patch("core.task_executor.log_to_agent_logs") as mock_log, patch(
            "core.task_executor.record_attempt"
        ), patch("core.task_executor.load_task_context", return_value={"attempts": []}), patch(
            "core.task_executor.build_agent_task", return_value="task"
        ), patch(
            "core.task_executor.load_prompt", return_value="prompt without schema marker"
        ):
            ok = executor.execute(task, pm_prompt="pm")

        assert ok is False
        mock_log.assert_called()
        log_kwargs = mock_log.call_args.kwargs
        assert log_kwargs["status"] == "error"
        assert log_kwargs["agent_name"] == "tech_writer"
        payload = json.loads(log_kwargs["full_response"])
        assert payload["raw_response"] == '{"weird": true, "documents": []}'
        assert payload["error"] == "llm_parse_or_validation_failed"

        # Последний update_task — reject с output_data
        reject_calls = [
            c for c in orch.tasks_db.update_task.call_args_list
            if isinstance(c[0][1], dict) and "output_data" in c[0][1]
        ]
        assert reject_calls
        assert "weird" in reject_calls[-1][0][1]["output_data"]
