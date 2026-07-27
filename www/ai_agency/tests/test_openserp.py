"""Тесты клиента OpenSERP (core/openserp_client.py)."""
from unittest.mock import MagicMock, patch

import pytest
import requests

from core.openserp_client import OpenSerpClient


def _response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("no json")
    return resp


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    """Без реальных sleep и с отключённым mega в большинстве тестов — явно включаем где нужно."""
    monkeypatch.setattr("core.openserp_client.time.sleep", lambda *_a, **_k: None)
    monkeypatch.setattr("core.config.Config.OPENSERP_MAX_RETRIES", 2)
    monkeypatch.setattr("core.config.Config.OPENSERP_USE_MEGA_FALLBACK", False)
    monkeypatch.setattr("core.config.Config.OPENSERP_FALLBACK_ENGINES", "")


class TestOpenSerpClientSearch:
    def test_not_configured_returns_empty(self, monkeypatch):
        # base_url="" не должен подставлять Config.OPENSERP_BASE_URL (живой OpenSERP)
        monkeypatch.setattr("core.config.Config.OPENSERP_BASE_URL", "http://localhost:7000")
        client = OpenSerpClient(base_url="")
        assert client.is_configured() is False
        assert client.search("query") == []

    def test_default_uses_config_base_url(self, monkeypatch):
        monkeypatch.setattr("core.config.Config.OPENSERP_BASE_URL", "http://openserp.test:7000")
        client = OpenSerpClient()
        assert client.base_url == "http://openserp.test:7000"
        assert client.is_configured() is True

    def test_empty_query_returns_empty(self):
        client = OpenSerpClient(base_url="http://localhost:7000")
        assert client.search("   ") == []

    @patch("core.openserp_client.requests.get")
    def test_parses_organic_results(self, mock_get):
        mock_get.return_value = _response(200, {
            "results": [
                {"type": "organic", "title": "A", "url": "https://a.example", "snippet": "s1"},
                {"type": "organic", "title": "B", "url": "https://b.example", "snippet": "s2"},
            ]
        })
        client = OpenSerpClient(base_url="http://localhost:7000")
        results = client.search("automation crm", limit=10)
        assert len(results) == 2
        assert results[0] == {"title": "A", "link": "https://a.example", "snippet": "s1"}

    @patch("core.openserp_client.requests.get")
    def test_filters_non_organic_results(self, mock_get):
        mock_get.return_value = _response(200, {
            "results": [
                {"type": "organic", "title": "A", "url": "https://a.example", "snippet": "s1"},
                {"type": "ad", "title": "Ad", "url": "https://ad.example", "snippet": "ad"},
            ]
        })
        client = OpenSerpClient(base_url="http://localhost:7000")
        results = client.search("query")
        assert len(results) == 1
        assert results[0]["link"] == "https://a.example"

    @patch("core.openserp_client.requests.get")
    def test_uses_configured_engine_in_url(self, mock_get):
        mock_get.return_value = _response(200, {"results": []})
        client = OpenSerpClient(base_url="http://localhost:7000")
        client.search("query", engine="yandex")
        called_url = mock_get.call_args[0][0]
        assert called_url == "http://localhost:7000/yandex/search"

    @patch("core.openserp_client.requests.get")
    def test_http_504_retries_then_succeeds(self, mock_get):
        mock_get.side_effect = [
            _response(504, text='{"error":"request_timeout"}'),
            _response(200, {
                "results": [
                    {"type": "organic", "title": "A", "url": "https://a.example", "snippet": "s"},
                ]
            }),
        ]
        client = OpenSerpClient(base_url="http://localhost:7000")
        results = client.search("клиника автоматизация")
        assert len(results) == 1
        assert mock_get.call_count == 2

    @patch("core.openserp_client.requests.get")
    def test_http_504_exhausted_returns_empty(self, mock_get):
        mock_get.return_value = _response(504, text="deadline exceeded")
        client = OpenSerpClient(base_url="http://localhost:7000")
        assert client.search("query") == []
        # 1 + OPENSERP_MAX_RETRIES(2) = 3
        assert mock_get.call_count == 3

    @patch("core.openserp_client.requests.get")
    def test_mega_fallback_after_primary_fail(self, mock_get, monkeypatch):
        monkeypatch.setattr("core.config.Config.OPENSERP_USE_MEGA_FALLBACK", True)
        monkeypatch.setattr("core.config.Config.OPENSERP_FALLBACK_ENGINES", "bing,yandex")
        monkeypatch.setattr("core.config.Config.OPENSERP_MAX_RETRIES", 0)

        def side_effect(url, **kwargs):
            if "/google/search" in url:
                return _response(504, text="deadline")
            if "/mega/search" in url:
                return _response(200, {
                    "results": [
                        {"type": "organic", "title": "B", "url": "https://b.example", "snippet": "ok"},
                    ]
                })
            return _response(200, {"results": []})

        mock_get.side_effect = side_effect
        client = OpenSerpClient(base_url="http://localhost:7000")
        results = client.search("query", engine="google")
        assert len(results) == 1
        assert results[0]["link"] == "https://b.example"
        mega_calls = [c for c in mock_get.call_args_list if "/mega/search" in c[0][0]]
        assert mega_calls
        assert mega_calls[0].kwargs["params"]["mode"] == "any"

    @patch("core.openserp_client.requests.get")
    def test_connection_error_returns_empty_not_raises(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError("refused")
        client = OpenSerpClient(base_url="http://localhost:7000")
        assert client.search("query") == []

    @patch("core.openserp_client.requests.get")
    def test_timeout_returns_empty_not_raises(self, mock_get):
        mock_get.side_effect = requests.exceptions.Timeout("timed out")
        client = OpenSerpClient(base_url="http://localhost:7000")
        assert client.search("query") == []

    @patch("core.openserp_client.requests.get")
    def test_invalid_json_returns_empty(self, mock_get):
        mock_get.return_value = _response(200, json_data=None, text="not json")
        client = OpenSerpClient(base_url="http://localhost:7000")
        assert client.search("query") == []

    @patch("core.openserp_client.requests.get")
    def test_limit_is_clamped_between_1_and_100(self, mock_get):
        mock_get.return_value = _response(200, {"results": []})
        client = OpenSerpClient(base_url="http://localhost:7000")
        client.search("query", limit=500)
        assert mock_get.call_args.kwargs["params"]["limit"] == 100
        client.search("query", limit=0)
        assert mock_get.call_args.kwargs["params"]["limit"] == 1

    def test_singleton_instance_uses_config_defaults(self):
        from core.config import Config
        from core.openserp_client import openserp_client

        assert openserp_client.base_url == Config.OPENSERP_BASE_URL.rstrip("/")

    def test_default_timeout_is_five_minutes(self):
        from core.config import Config

        assert Config.OPENSERP_TIMEOUT_SEC == 300
        client = OpenSerpClient(base_url="http://localhost:7000")
        assert client.timeout == 300

    @patch("core.openserp_client.requests.get")
    def test_search_uses_configured_timeout(self, mock_get):
        mock_get.return_value = _response(200, {"results": []})
        client = OpenSerpClient(base_url="http://localhost:7000", timeout=300)
        client.search("query")
        assert mock_get.call_args.kwargs["timeout"] == 300
