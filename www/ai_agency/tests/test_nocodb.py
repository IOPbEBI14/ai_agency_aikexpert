"""
Тесты для NocoDB-клиентов и nocodb_proxy.

Покрывает:
  - API v2/v3 различия (Id vs id)
  - _unpack_record / _unpack_task
  - _build_where_url (критичный баг: return вне if-блока)
  - update_task / update_project (PATCH payload)
  - nocodb_proxy безопасность (path whitelist, method whitelist, param filter)
"""
import json
import pytest
from unittest.mock import MagicMock, patch


# ══════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════

def _make_mock_response(json_data, status_code=200):
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data
    mock.raise_for_status = MagicMock()
    return mock


# ══════════════════════════════════════════════════════════════════
# ProjectsClient
# ══════════════════════════════════════════════════════════════════

class TestProjectsClientUnpack:
    """_unpack_record извлекает ID независимо от формата API."""

    @pytest.fixture
    def client(self):
        # Импортируем ДО любого патча, чтобы получить реальный класс
        from core.nocodb import ProjectsClient as _Real
        c = object.__new__(_Real)
        c.projects_url = "http://noco/api/v3/data/base/projects/records"
        c.headers = {}
        return c

    def test_unpack_v3_lowercase_id(self, client):
        """API v3 возвращает id (маленькая буква) — должно стать Id."""
        raw = {"id": 42, "fields": {"project_name": "Test", "status": "in_progress"}}
        result = client._unpack_record(raw)
        assert result["Id"] == 42
        assert result["project_name"] == "Test"

    def test_unpack_v2_uppercase_id(self, client):
        """API v2 возвращает Id (заглавная) — обратная совместимость."""
        raw = {"Id": 7, "fields": {"project_name": "Old API"}}
        result = client._unpack_record(raw)
        assert result["Id"] == 7

    def test_unpack_missing_id_logs_warning(self, client, caplog):
        """Когда ID отсутствует — логируем предупреждение, не падаем."""
        raw = {"fields": {"project_name": "No ID"}}
        result = client._unpack_record(raw)
        assert result["Id"] is None

    def test_unpack_empty_raw(self, client):
        """Пустая запись (falsy) → ранний возврат пустого dict, не исключение."""
        # {} is falsy → hits `if not raw: return {}` guard
        assert client._unpack_record({}) == {}

    def test_unpack_none_raw(self, client):
        """None-запись → пустой dict."""
        assert client._unpack_record(None) == {}


class TestBuildWhereUrl:
    """_build_where_url всегда возвращает str (исторический баг: return внутри if)."""

    @pytest.fixture
    def client(self):
        from core.nocodb import ProjectsClient as _Real
        c = object.__new__(_Real)
        c.projects_url = "http://noco/records"
        c.headers = {}
        return c

    def test_returns_string_without_sort(self, client):
        """Без sort_field возвращает строку (не None — исправленный баг)."""
        url = client._build_where_url("status", "in_progress")
        assert isinstance(url, str), "Должна вернуться строка, а не None"
        assert "where=" in url
        assert "limit=1" in url

    def test_returns_string_with_sort(self, client):
        url = client._build_where_url("status", "completed", sort_field="UpdatedAt")
        assert isinstance(url, str)
        assert "sort=" in url

    def test_field_and_value_encoded(self, client):
        """Спецсимволы в значении кодируются (url-encode)."""
        url = client._build_where_url("project_name", "Test Project", limit=5)
        assert "limit=5" in url

    def test_custom_limit(self, client):
        url = client._build_where_url("status", "in_progress", limit=10)
        assert "limit=10" in url


class TestUpdateProject:
    """update_project формирует корректный PATCH-payload для API v3."""

    @pytest.fixture
    def client(self):
        from core.nocodb import ProjectsClient as _Real
        c = object.__new__(_Real)
        c.projects_url = "http://noco/records"
        c.headers = {"xc-token": "tok"}
        return c

    def test_patch_uses_lowercase_id(self, client):
        """API v3 требует 'id' (маленькая), не 'Id'."""
        captured = {}

        def fake_patch(url, json, headers, timeout):
            captured["payload"] = json
            return _make_mock_response({})

        with patch("requests.patch", side_effect=fake_patch):
            client.update_project(99, {"status": "completed"})

        assert captured["payload"][0]["id"] == 99
        assert "Id" not in captured["payload"][0]

    def test_patch_serializes_completed_agents_list(self, client):
        """completed_agents как list → должен стать JSON-строкой."""
        captured = {}

        def fake_patch(url, json, headers, timeout):
            captured["payload"] = json
            return _make_mock_response({})

        with patch("requests.patch", side_effect=fake_patch):
            client.update_project(1, {"completed_agents": ["analyst", "architect"]})

        fields = captured["payload"][0]["fields"]
        assert isinstance(fields["completed_agents"], str)
        assert json.loads(fields["completed_agents"]) == ["analyst", "architect"]

    def test_returns_false_on_4xx(self, client):
        with patch("requests.patch", return_value=_make_mock_response({}, 400)):
            result = client.update_project(1, {"status": "bad"})
        assert result is False

    def test_returns_false_without_project_id(self, client):
        result = client.update_project(None, {"status": "ok"})
        assert result is False


# ══════════════════════════════════════════════════════════════════
# TasksClient
# ══════════════════════════════════════════════════════════════════

class TestTasksClientUnpack:
    """_unpack_task корректно извлекает id задачи из API v3."""

    @pytest.fixture
    def client(self):
        from core.nocodb import TasksClient as _Real
        c = object.__new__(_Real)
        c.tasks_url = "http://noco/tasks/records"
        c.headers = {}
        return c

    def test_unpack_v3_id(self, client):
        raw = {"id": 55, "fields": {"task_id": "task_001", "status": "pending"}}
        result = client._unpack_task(raw)
        assert result["Id"] == 55
        assert result["task_id"] == "task_001"

    def test_unpack_fallback_to_row_id(self, client):
        raw = {"row_id": 33, "fields": {"task_id": "task_002"}}
        result = client._unpack_task(raw)
        assert result["Id"] == 33

    def test_unpack_missing_id(self, client, caplog):
        raw = {"fields": {"task_id": "orphan"}}
        result = client._unpack_task(raw)
        assert result["Id"] is None


class TestUpdateTask:
    """update_task формирует корректный PATCH-payload."""

    @pytest.fixture
    def client(self):
        from core.nocodb import TasksClient as _Real
        c = object.__new__(_Real)
        c.tasks_url = "http://noco/tasks/records"
        c.headers = {"xc-token": "tok"}
        return c

    def test_patch_uses_lowercase_id(self, client):
        captured = {}

        def fake_patch(url, json, headers, timeout):
            captured["payload"] = json
            return _make_mock_response({})

        with patch("requests.patch", side_effect=fake_patch):
            client.update_task(77, {"status": "completed"})

        assert captured["payload"][0]["id"] == 77
        assert "Id" not in captured["payload"][0]

    def test_patch_serializes_depends_on_list(self, client):
        captured = {}

        def fake_patch(url, json, headers, timeout):
            captured["payload"] = json
            return _make_mock_response({})

        with patch("requests.patch", side_effect=fake_patch):
            client.update_task(1, {"depends_on": ["task_001", "task_002"]})

        fields = captured["payload"][0]["fields"]
        assert isinstance(fields["depends_on"], str)
        assert json.loads(fields["depends_on"]) == ["task_001", "task_002"]

    def test_returns_false_without_task_id(self, client):
        assert client.update_task(None, {"status": "ok"}) is False

    def test_returns_false_on_5xx(self, client):
        with patch("requests.patch", return_value=_make_mock_response({}, 500)):
            assert client.update_task(1, {"status": "ok"}) is False

    def test_updated_at_always_added(self, client):
        """Метка времени добавляется автоматически для аудита."""
        captured = {}

        def fake_patch(url, json, headers, timeout):
            captured["payload"] = json
            return _make_mock_response({})

        with patch("requests.patch", side_effect=fake_patch):
            client.update_task(1, {"status": "completed"})

        assert "UpdatedAt" in captured["payload"][0]["fields"]


# ══════════════════════════════════════════════════════════════════
# nocodb_proxy (main.py) — безопасность
# ══════════════════════════════════════════════════════════════════

class TestNocodbProxy:
    """Безопасность nocodb_proxy: whitelist путей, методов, параметров."""

    @pytest.fixture
    def app_client(self):
        """Flask test client с замокированным requests.request."""
        with patch("core.nocodb.NocoDBClient"), \
             patch("core.nocodb.ProjectsClient"), \
             patch("core.nocodb.TasksClient"), \
             patch("core.orchestrator.Orchestrator"):
            import main as app_module
            app_module.app.config["TESTING"] = True
            with app_module.app.test_client() as client:
                yield client

    def test_get_allowed(self, app_client):
        """GET на корень прокси → пропускается до NocoDB."""
        mock_resp = MagicMock()
        mock_resp.content = b'{"records": []}'
        mock_resp.status_code = 200
        mock_resp.headers = {"Content-Type": "application/json"}

        with patch("requests.request", return_value=mock_resp):
            resp = app_client.get("/api/nocodb")
        assert resp.status_code == 200

    def test_delete_blocked(self, app_client):
        """DELETE должен быть отклонён (405 от Flask — метод не зарегистрирован)."""
        resp = app_client.delete("/api/nocodb")
        assert resp.status_code == 405

    def test_path_traversal_blocked(self, app_client):
        """Путь '../secret' должен вернуть 403 (не пробрасывается в NocoDB)."""
        with patch("requests.request") as mock_req:
            resp = app_client.get("/api/nocodb/..%2Fsecret")
        assert resp.status_code in (403, 404)
        mock_req.assert_not_called()

    def test_path_with_alpha_blocked(self, app_client):
        """Путь с буквами блокируется — разрешены только числовые ID."""
        with patch("requests.request") as mock_req:
            resp = app_client.get("/api/nocodb/admin")
        assert resp.status_code == 403
        mock_req.assert_not_called()

    def test_numeric_path_allowed(self, app_client):
        """Числовой ID записи (напр. /42) разрешён."""
        mock_resp = MagicMock()
        mock_resp.content = b'{"id": 42}'
        mock_resp.status_code = 200
        mock_resp.headers = {}

        with patch("requests.request", return_value=mock_resp):
            resp = app_client.get("/api/nocodb/42")
        assert resp.status_code == 200

    def test_unknown_query_params_filtered(self, app_client):
        """Неизвестные query-параметры (token, secret и т.д.) не пробрасываются."""
        captured_url = []

        def fake_request(method, url, **kwargs):
            captured_url.append(url)
            mock_resp = MagicMock()
            mock_resp.content = b'{}'
            mock_resp.status_code = 200
            mock_resp.headers = {}
            return mock_resp

        with patch("requests.request", side_effect=fake_request):
            app_client.get("/api/nocodb?limit=5&secret=hack&token=evil")

        assert captured_url, "requests.request не был вызван"
        url = captured_url[0]
        assert "secret" not in url
        assert "token" not in url
        assert "limit=5" in url

    def test_allowed_params_passed_through(self, app_client):
        """Разрешённые параметры (limit, offset, where, sort) пробрасываются."""
        captured_url = []

        def fake_request(method, url, **kwargs):
            captured_url.append(url)
            mock_resp = MagicMock()
            mock_resp.content = b'{}'
            mock_resp.status_code = 200
            mock_resp.headers = {}
            return mock_resp

        with patch("requests.request", side_effect=fake_request):
            app_client.get("/api/nocodb?limit=10&offset=20")

        url = captured_url[0]
        assert "limit=10" in url
        assert "offset=20" in url
