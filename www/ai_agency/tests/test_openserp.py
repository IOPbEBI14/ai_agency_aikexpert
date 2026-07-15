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


class TestOpenSerpClientSearch:
    def test_not_configured_returns_empty(self):
        client = OpenSerpClient(base_url="")
        assert client.search("query") == []

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
    def test_http_error_returns_empty(self, mock_get):
        mock_get.return_value = _response(503, text="service_unavailable")
        client = OpenSerpClient(base_url="http://localhost:7000")
        assert client.search("query") == []

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
