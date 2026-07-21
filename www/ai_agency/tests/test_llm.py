"""
Тесты для функции call_llm() / llm_engine с моками.
"""
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from core.utils import call_llm, try_fix_truncated_json


@pytest.fixture(autouse=True)
def _force_yandex_provider(monkeypatch):
    """Тесты call_llm завязаны на формат Yandex Responses API."""
    import core.llm_engine as eng
    from core.config import Config

    monkeypatch.setenv("LLM_PROVIDER", "yandexgpt")
    monkeypatch.setenv("YANDEX_API_KEY", "test-yandex-key")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "test-folder")
    monkeypatch.setenv("YANDEX_MODEL", "yandexgpt")
    monkeypatch.setattr(Config, "LLM_API_KEY", "test-yandex-key")
    monkeypatch.setattr(Config, "LLM_FOLDER_ID", "test-folder")
    monkeypatch.setattr(Config, "LLM_MODEL", "yandexgpt")
    eng._active_provider_id = "yandexgpt"


class TestCallLLM:
    """Тесты функции call_llm()."""

    @patch("core.llm_engine.requests.post")
    def test_successful_call(self, mock_post, mock_llm_response):
        """Тест успешного вызова LLM."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_llm_response
        mock_post.return_value = mock_response

        content, tokens = call_llm("test_agent", "system prompt", "user task")

        assert content == '{"status": "ok", "data": "test"}'
        assert tokens == 100
        mock_post.assert_called_once()

    @patch("core.llm_engine.requests.post")
    def test_call_with_timeout_retry(self, mock_post, mock_llm_response):
        """Тест повторной попытки при таймауте."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = mock_llm_response

        mock_post.side_effect = [
            requests.exceptions.Timeout(),
            mock_response,
        ]

        content, tokens = call_llm("test_agent", "system", "task", max_retries=2)

        assert content == '{"status": "ok", "data": "test"}'
        assert mock_post.call_count == 2

    @patch("core.llm_engine.requests.post")
    def test_call_with_api_error(self, mock_post):
        """Тест обработки ошибки API."""
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.text = "Internal Server Error"
        mock_post.return_value = mock_response

        with pytest.raises(RuntimeError, match="500"):
            call_llm("test_agent", "system", "task", max_retries=0)

    @patch("core.llm_engine.requests.post")
    def test_call_with_truncated_response(self, mock_post):
        """Тест обработки обрезанного ответа."""
        truncated_response = {
            "output_text": '{"key": "value"',
            "usage": {"total_tokens": 50},
        }

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = truncated_response
        mock_post.return_value = mock_response

        content, tokens = call_llm("test_agent", "system", "task", max_retries=2)

        assert '{"key": "value"}' in content

    @patch("core.llm_engine.requests.post")
    def test_call_increases_max_tokens_on_truncation(self, mock_post, mock_llm_response):
        """Тест увеличения max_tokens при обрезанном ответе."""
        truncated_response = {
            "output_text": '{"key": "value"',
            "usage": {"total_tokens": 50},
        }

        mock_response_1 = MagicMock()
        mock_response_1.status_code = 200
        mock_response_1.json.return_value = truncated_response

        mock_response_2 = MagicMock()
        mock_response_2.status_code = 200
        mock_response_2.json.return_value = mock_llm_response

        mock_post.side_effect = [mock_response_1, mock_response_2]

        call_llm("test_agent", "system", "task", max_retries=2)

        second_call_kwargs = mock_post.call_args_list[1][1]
        assert second_call_kwargs["json"]["max_tokens"] > 16000


class TestTryFixTruncatedJson:
    """Тесты функции восстановления обрезанного JSON."""

    def test_fix_missing_closing_brace(self):
        content = '{"key": "value"'
        result = try_fix_truncated_json(content)
        assert result == '{"key": "value"}'

    def test_fix_missing_closing_bracket(self):
        content = '{"key": [1, 2'
        result = try_fix_truncated_json(content)
        assert result == '{"key": [1, 2]}'

    def test_fix_nested_braces(self):
        content = '{"outer": {"inner": "value"'
        result = try_fix_truncated_json(content)
        assert result == '{"outer": {"inner": "value"}}'

    def test_fix_by_removing_last_comma(self):
        content = '{"key": "value", "other":'
        result = try_fix_truncated_json(content)
        assert result is not None
        json.loads(result)

    def test_fix_empty_string(self):
        result = try_fix_truncated_json("")
        assert result == ""

    def test_fix_non_json_string(self):
        result = try_fix_truncated_json("Просто текст")
        assert result == ""

    def test_fix_already_valid_json(self):
        content = '{"key": "value"}'
        result = try_fix_truncated_json(content)
        assert result == content

    def test_fix_complex_nested_structure(self):
        content = '{"a": {"b": [1, 2, {"c": "d"'
        result = try_fix_truncated_json(content)
        if result:
            try:
                json.loads(result)
            except json.JSONDecodeError:
                pytest.fail("Восстановленный JSON невалиден")
        else:
            assert result == ""
