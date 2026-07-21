"""Тесты мульти-провайдерного LLM-движка."""
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_provider(monkeypatch):
    import core.llm_engine as eng
    from core.config import Config

    monkeypatch.setenv("LLM_PROVIDER", "yandexgpt")
    monkeypatch.setenv("YANDEX_API_KEY", "yandex-key-xxxx")
    monkeypatch.setenv("YANDEX_FOLDER_ID", "folder-1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-test")
    monkeypatch.setenv("GROK_API_KEY", "xai-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anth-test")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-test")
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "basic dGVzdA==")
    monkeypatch.setattr(Config, "LLM_API_KEY", "yandex-key-xxxx")
    monkeypatch.setattr(Config, "LLM_FOLDER_ID", "folder-1")
    monkeypatch.setattr(Config, "LLM_MODEL", "yandexgpt")
    eng._active_provider_id = None
    eng._gigachat_token = None
    eng._gigachat_token_expires = 0.0
    yield
    eng._active_provider_id = None


class TestLlmEngineProviders:
    def test_list_providers_marks_configured(self):
        from core import llm_engine as eng

        items = eng.list_providers()
        ids = [p["id"] for p in items]
        assert ids == list(eng.PROVIDER_ORDER)
        by_id = {p["id"]: p for p in items}
        assert by_id["openai"]["configured"] is True
        assert by_id["yandexgpt"]["configured"] is True
        assert by_id["yandexgpt"]["active"] is True

    def test_set_active_provider(self):
        from core import llm_engine as eng

        status = eng.set_active_provider("openai")
        assert status["provider"] == "openai"
        assert status["label"] == "OpenAI"
        assert eng.get_active_provider_id() == "openai"

    def test_set_unknown_provider(self):
        from core import llm_engine as eng

        with pytest.raises(ValueError, match="Неизвестный"):
            eng.set_active_provider("unknown_ai")

    def test_set_unconfigured_provider(self, monkeypatch):
        from core import llm_engine as eng

        monkeypatch.delenv("GROK_API_KEY", raising=False)
        with pytest.raises(ValueError, match="не настроен"):
            eng.set_active_provider("grok")

    @patch("core.llm_engine.requests.post")
    def test_openai_chat_invoke(self, mock_post):
        from core import llm_engine as eng

        eng.set_active_provider("openai")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"total_tokens": 12},
        }
        mock_post.return_value = mock_response

        content, tokens = eng.invoke("a", "sys", "user", max_retries=0)
        assert content == "hello"
        assert tokens == 12
        url = mock_post.call_args[0][0]
        assert "chat/completions" in url

    @patch("core.llm_engine.requests.post")
    def test_anthropic_invoke(self, mock_post):
        from core import llm_engine as eng

        eng.set_active_provider("anthropic")
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "content": [{"type": "text", "text": "claude-ok"}],
            "usage": {"input_tokens": 3, "output_tokens": 4},
        }
        mock_post.return_value = mock_response

        content, tokens = eng.invoke("a", "sys", "user", max_retries=0)
        assert content == "claude-ok"
        assert tokens == 7
        headers = mock_post.call_args[1]["headers"]
        assert "x-api-key" in headers
