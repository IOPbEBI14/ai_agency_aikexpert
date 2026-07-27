"""Тесты n8n-validator: heuristic + official engine + публичный API."""
from unittest.mock import MagicMock, patch

import pytest

from core.n8n_validator import (
    build_n8n_feedback,
    validate_n8n_workflow,
    validate_n8n_workflow_heuristic,
    validate_n8n_workflow_official,
)


def _base_workflow(nodes=None, connections=None):
    nodes = nodes or [
        {
            "name": "Schedule Trigger",
            "type": "n8n-nodes-base.scheduleTrigger",
            "typeVersion": 1.2,
            "position": [0, 0],
            "parameters": {
                "rule": {"interval": [{"field": "minutes", "minutesInterval": 10}]}
            },
        },
        {
            "name": "HTTP Request",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [200, 0],
            "parameters": {"method": "GET", "url": "https://example.com"},
        },
    ]
    if connections is None:
        connections = {
            "Schedule Trigger": {
                "main": [[{"node": "HTTP Request", "type": "main", "index": 0}]]
            }
        }
    return {"name": "Test", "nodes": nodes, "connections": connections}


@pytest.fixture(autouse=True)
def disable_external_validators(monkeypatch):
    """По умолчанию тесты heuristic не зависят от Node/npx."""
    monkeypatch.setenv("N8N_VALIDATOR_RUNTIME", "off")
    monkeypatch.setenv("N8N_VALIDATOR_OFFICIAL", "off")
    import core.n8n_validator as mod
    monkeypatch.setattr(mod, "_local_checked", False)
    monkeypatch.setattr(mod, "_local_bin", None)
    monkeypatch.setattr(mod, "_official_checked", False)
    monkeypatch.setattr(mod, "_official_cmd", None)


class TestHeuristicHappyPath:
    def test_valid_minimal_workflow(self):
        ok, issues = validate_n8n_workflow_heuristic(_base_workflow())
        assert ok is True
        assert issues == []

    def test_public_api_valid(self):
        ok, issues = validate_n8n_workflow(_base_workflow())
        assert ok is True
        assert issues == []


class TestImportKillers:
    def test_schedule_interval_not_array(self):
        wf = _base_workflow()
        wf["nodes"][0]["parameters"]["rule"] = {"interval": 10}
        ok, issues = validate_n8n_workflow_heuristic(wf)
        assert ok is False
        assert any("interval" in i and "массивом" in i for i in issues)

    def test_input_index_instead_of_index(self):
        wf = _base_workflow(
            connections={
                "Schedule Trigger": {
                    "main": [[{
                        "node": "HTTP Request",
                        "type": "main",
                        "inputIndex": 0,
                    }]]
                }
            }
        )
        ok, issues = validate_n8n_workflow_heuristic(wf)
        assert ok is False
        assert any("inputIndex" in i for i in issues)

    def test_missing_index_in_connection(self):
        wf = _base_workflow(
            connections={
                "Schedule Trigger": {
                    "main": [[{"node": "HTTP Request", "type": "main"}]]
                }
            }
        )
        ok, issues = validate_n8n_workflow_heuristic(wf)
        assert ok is False
        assert any("index" in i for i in issues)

    def test_isolated_node(self):
        wf = _base_workflow()
        wf["nodes"].append({
            "name": "Orphan",
            "type": "n8n-nodes-base.set",
            "typeVersion": 3.4,
            "position": [400, 0],
            "parameters": {"assignments": {"assignments": []}},
        })
        ok, issues = validate_n8n_workflow_heuristic(wf)
        assert ok is False
        assert any("изолирована" in i or "Orphan" in i for i in issues)

    def test_empty_if_conditions(self):
        wf = _base_workflow(
            nodes=[
                {
                    "name": "Webhook",
                    "type": "n8n-nodes-base.webhook",
                    "typeVersion": 1,
                    "position": [0, 0],
                    "parameters": {"path": "hook"},
                },
                {
                    "name": "Check",
                    "type": "n8n-nodes-base.if",
                    "typeVersion": 2.2,
                    "position": [200, 0],
                    "parameters": {
                        "conditions": {"combinator": "and", "conditions": []},
                    },
                },
                {
                    "name": "OK",
                    "type": "n8n-nodes-base.set",
                    "typeVersion": 3.4,
                    "position": [400, 0],
                    "parameters": {"assignments": {"assignments": []}},
                },
                {
                    "name": "Fail",
                    "type": "n8n-nodes-base.set",
                    "typeVersion": 3.4,
                    "position": [400, 200],
                    "parameters": {"assignments": {"assignments": []}},
                },
            ],
            connections={
                "Webhook": {"main": [[{"node": "Check", "type": "main", "index": 0}]]},
                "Check": {
                    "main": [
                        [{"node": "OK", "type": "main", "index": 0}],
                        [{"node": "Fail", "type": "main", "index": 0}],
                    ]
                },
            },
        )
        ok, issues = validate_n8n_workflow_heuristic(wf)
        assert ok is False
        assert any("пуст" in i for i in issues)

    def test_if_missing_false_branch(self):
        wf = _base_workflow(
            nodes=[
                {
                    "name": "Webhook",
                    "type": "n8n-nodes-base.webhook",
                    "typeVersion": 1,
                    "position": [0, 0],
                    "parameters": {"path": "hook"},
                },
                {
                    "name": "Check",
                    "type": "n8n-nodes-base.if",
                    "typeVersion": 2.2,
                    "position": [200, 0],
                    "parameters": {
                        "conditions": {
                            "combinator": "and",
                            "conditions": [{
                                "leftValue": "={{ $json.ok }}",
                                "rightValue": True,
                                "operator": {"type": "boolean", "operation": "equals"},
                            }],
                        }
                    },
                },
                {
                    "name": "OK",
                    "type": "n8n-nodes-base.set",
                    "typeVersion": 3.4,
                    "position": [400, 0],
                    "parameters": {"assignments": {"assignments": []}},
                },
            ],
            connections={
                "Webhook": {"main": [[{"node": "Check", "type": "main", "index": 0}]]},
                "Check": {
                    "main": [[{"node": "OK", "type": "main", "index": 0}]]
                },
            },
        )
        ok, issues = validate_n8n_workflow_heuristic(wf)
        assert ok is False
        assert any("обе ветки" in i for i in issues)

    def test_nocodb_data_dict_blocked(self):
        wf = _base_workflow()
        wf["nodes"].append({
            "name": "Noco Update",
            "type": "n8n-nodes-base.nocoDb",
            "typeVersion": 2,
            "position": [400, 0],
            "parameters": {
                "operation": "update",
                "tableId": "t1",
                "rowId": "1",
                "data": {"synced": True},
            },
            "credentials": {"nocoDbApiToken": {"id": "1", "name": "Noco"}},
        })
        wf["connections"]["HTTP Request"] = {
            "main": [[{"node": "Noco Update", "type": "main", "index": 0}]]
        }
        ok, issues = validate_n8n_workflow_heuristic(wf)
        assert ok is False
        assert any("data" in i and "fieldsUi" in i for i in issues)

    def test_wrong_credential_key(self):
        wf = _base_workflow()
        wf["nodes"][1]["credentials"] = {"NocoDB": {"id": "1", "name": "x"}}
        # httpRequest with wrong key
        ok, issues = validate_n8n_workflow_heuristic(wf)
        assert ok is False
        assert any("credentials" in i and "NocoDB" in i for i in issues)

    def test_empty_options_on_if(self):
        wf = _base_workflow(
            nodes=[
                {
                    "name": "Webhook",
                    "type": "n8n-nodes-base.webhook",
                    "typeVersion": 1,
                    "position": [0, 0],
                    "parameters": {"path": "x"},
                },
                {
                    "name": "Check",
                    "type": "n8n-nodes-base.if",
                    "typeVersion": 2.2,
                    "position": [200, 0],
                    "parameters": {
                        "options": {},
                        "conditions": {
                            "combinator": "and",
                            "conditions": [{
                                "leftValue": "={{ $json.a }}",
                                "rightValue": 1,
                                "operator": {"type": "number", "operation": "equals"},
                            }],
                        },
                    },
                },
                {
                    "name": "A",
                    "type": "n8n-nodes-base.set",
                    "typeVersion": 3.4,
                    "position": [400, 0],
                    "parameters": {"assignments": {"assignments": []}},
                },
                {
                    "name": "B",
                    "type": "n8n-nodes-base.set",
                    "typeVersion": 3.4,
                    "position": [400, 200],
                    "parameters": {"assignments": {"assignments": []}},
                },
            ],
            connections={
                "Webhook": {"main": [[{"node": "Check", "type": "main", "index": 0}]]},
                "Check": {
                    "main": [
                        [{"node": "A", "type": "main", "index": 0}],
                        [{"node": "B", "type": "main", "index": 0}],
                    ]
                },
            },
        )
        ok, issues = validate_n8n_workflow_heuristic(wf)
        assert ok is False
        assert any("options" in i for i in issues)

    def test_duplicate_node_names(self):
        wf = _base_workflow()
        wf["nodes"].append({
            "name": "HTTP Request",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [400, 0],
            "parameters": {"url": "https://b.example"},
        })
        ok, issues = validate_n8n_workflow_heuristic(wf)
        assert ok is False
        assert any("Дублируется" in i for i in issues)

    def test_missing_connections_field(self):
        wf = {"name": "X", "nodes": _base_workflow()["nodes"]}
        ok, issues = validate_n8n_workflow_heuristic(wf)
        assert ok is False
        assert any("connections" in i for i in issues)

    def test_webhook_without_path(self):
        wf = _base_workflow(
            nodes=[{
                "name": "Webhook",
                "type": "n8n-nodes-base.webhook",
                "typeVersion": 1,
                "position": [0, 0],
                "parameters": {},
            }],
            connections={},
        )
        ok, issues = validate_n8n_workflow_heuristic(wf)
        assert ok is False
        assert any("path" in i for i in issues)


class TestPublicApiBlocking:
    def test_heuristic_errors_block_even_without_runtime(self):
        wf = _base_workflow()
        wf["nodes"][0]["parameters"]["rule"] = {"interval": 5}
        ok, issues = validate_n8n_workflow(wf)
        assert ok is False
        assert issues

    def test_build_n8n_feedback(self):
        text = build_n8n_feedback(["problem 1", "problem 2"])
        assert "НЕ пройдёт" in text
        assert "problem 1" in text
        assert "problem 2" in text

    def test_non_dict_rejected(self):
        ok, issues = validate_n8n_workflow([])
        assert ok is False
        assert issues


class TestOfficialEngineLayer:
    """Layer C: официальный n8n-workflow-validator как внешний шаг."""

    def test_official_off_returns_none(self, monkeypatch):
        monkeypatch.setenv("N8N_VALIDATOR_OFFICIAL", "off")
        import core.n8n_validator as mod
        monkeypatch.setattr(mod, "_official_checked", False)
        monkeypatch.setattr(mod, "_official_cmd", None)
        ok, issues = validate_n8n_workflow_official(_base_workflow())
        assert ok is None
        assert issues == []

    def test_official_on_unavailable_fails(self, monkeypatch):
        monkeypatch.setenv("N8N_VALIDATOR_OFFICIAL", "on")
        import core.n8n_validator as mod
        monkeypatch.setattr(mod, "_official_checked", True)
        monkeypatch.setattr(mod, "_official_cmd", None)
        ok, issues = validate_n8n_workflow_official(_base_workflow())
        assert ok is False
        assert any("обязателен" in i for i in issues)

    def test_official_errors_block_public_api(self, monkeypatch):
        monkeypatch.setenv("N8N_VALIDATOR_OFFICIAL", "auto")
        import core.n8n_validator as mod

        def fake_official(_wf):
            return False, ["[ERROR] [Switch] N8N_PARAMETER_VALIDATION_ERROR: Could not find property option"]

        monkeypatch.setattr(mod, "validate_n8n_workflow_official", fake_official)
        monkeypatch.setattr(mod, "validate_n8n_workflow_local", lambda _w: (None, []))
        ok, issues = validate_n8n_workflow(_base_workflow())
        assert ok is False
        assert any("N8N_PARAMETER_VALIDATION_ERROR" in i for i in issues)

    def test_parse_official_json_with_schema_delta(self):
        from core.n8n_validator import _parse_validator_output

        payload = {
            "valid": False,
            "issues": [{
                "code": "N8N_PARAMETER_VALIDATION_ERROR",
                "severity": "error",
                "message": "Could not find property option",
                "location": {"nodeName": "route-by-format", "nodeType": "n8n-nodes-base.switch"},
                "context": {
                    "n8nError": "Could not find property option",
                    "schemaDelta": {"missingKeys": ["options"], "extraKeys": ["fallbackOutput"]},
                },
            }],
        }
        issues = _parse_validator_output(__import__("json").dumps(payload), "")
        assert len(issues) == 1
        assert "route-by-format" in issues[0]
        assert "missing=" in issues[0]
        assert "extra=" in issues[0]

    def test_smoke_blocks_schema_delta_on_exit_0(self, monkeypatch):
        """Exit 0 + schemaDelta.missingKeys → fail (import smoke)."""
        import core.n8n_validator as mod

        def fake_run(cmd, **kwargs):
            m = MagicMock()
            m.returncode = 0
            m.stdout = __import__("json").dumps({
                "valid": True,
                "issues": [{
                    "code": "N8N_PARAMETER_VALIDATION_ERROR",
                    "severity": "error",
                    "message": "Could not find property option",
                    "location": {"nodeName": "Switch"},
                    "context": {"schemaDelta": {"missingKeys": ["options"]}},
                }],
            })
            m.stderr = ""
            return m

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        ok, issues = mod._run_cli_validator(
            ["npx", "--yes", "n8n-workflow-validator"],
            _base_workflow(),
            timeout=10,
            label="Official",
        )
        assert ok is False
        assert any("missing=" in i for i in issues)

    def test_run_cli_passes_json_flag_for_official(self, monkeypatch):
        import core.n8n_validator as mod

        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            m = MagicMock()
            m.returncode = 0
            m.stdout = '{"valid": true, "issues": []}'
            m.stderr = ""
            return m

        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        ok, issues = mod._run_cli_validator(
            ["npx", "--yes", "n8n-workflow-validator"],
            _base_workflow(),
            timeout=10,
            label="Official",
        )
        assert ok is True
        assert "--json" in captured["cmd"]
        assert captured["cmd"][0] == "npx"


class TestDirectionKResilience:
    """Фаза 1: критичные HTTP/NocoDB без retry/error-ветки → ERROR."""

    def _mutating_http(self, **extra):
        node = {
            "name": "HTTP Create",
            "type": "n8n-nodes-base.httpRequest",
            "typeVersion": 4.2,
            "position": [200, 0],
            "parameters": {
                "method": "POST",
                "url": "https://api.example.com/items",
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": "={{ JSON.stringify({ name: $json.name }) }}",
            },
        }
        node.update(extra)
        return node

    def test_post_without_retry_blocked(self):
        wf = _base_workflow(
            nodes=[
                {
                    "name": "Schedule Trigger",
                    "type": "n8n-nodes-base.scheduleTrigger",
                    "typeVersion": 1.2,
                    "position": [0, 0],
                    "parameters": {
                        "rule": {"interval": [{"field": "minutes", "minutesInterval": 10}]}
                    },
                },
                self._mutating_http(),
            ],
            connections={
                "Schedule Trigger": {
                    "main": [[{"node": "HTTP Create", "type": "main", "index": 0}]]
                }
            },
        )
        ok, issues = validate_n8n_workflow_heuristic(wf)
        assert ok is False
        assert any("Direction K" in i and "retryOnFail" in i for i in issues)

    def test_post_with_full_resilience_ok(self):
        http = self._mutating_http(
            retryOnFail=True,
            maxTries=3,
            waitBetweenTries=2000,
            onError="continueErrorOutput",
            notes="idempotency_key = external_id from source",
            parameters={
                "method": "POST",
                "url": "https://api.example.com/items",
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": (
                    "={{ JSON.stringify({ external_id: $json.id, name: $json.name }) }}"
                ),
            },
        )
        wf = _base_workflow(
            nodes=[
                {
                    "name": "Schedule Trigger",
                    "type": "n8n-nodes-base.scheduleTrigger",
                    "typeVersion": 1.2,
                    "position": [0, 0],
                    "parameters": {
                        "rule": {"interval": [{"field": "minutes", "minutesInterval": 10}]}
                    },
                },
                http,
                {
                    "name": "Log Error",
                    "type": "n8n-nodes-base.set",
                    "typeVersion": 3.4,
                    "position": [400, 200],
                    "parameters": {"assignments": {"assignments": []}},
                },
            ],
            connections={
                "Schedule Trigger": {
                    "main": [[{"node": "HTTP Create", "type": "main", "index": 0}]]
                },
                "HTTP Create": {
                    "main": [[]],
                    "error": [[{"node": "Log Error", "type": "main", "index": 0}]],
                },
            },
        )
        ok, issues = validate_n8n_workflow_heuristic(wf)
        assert ok is True, issues

    def test_get_http_skips_resilience(self):
        """GET не мутирует — Direction K не требует retry."""
        ok, issues = validate_n8n_workflow_heuristic(_base_workflow())
        assert ok is True
        assert not any("Direction K" in i for i in issues)

    def test_nocodb_update_needs_retry_and_error(self):
        wf = _base_workflow()
        wf["nodes"].append({
            "name": "Noco Update",
            "type": "n8n-nodes-base.nocoDb",
            "typeVersion": 2,
            "position": [400, 0],
            "parameters": {
                "operation": "update",
                "tableId": "t1",
                "rowId": "1",
                "fieldsUi": {
                    "fieldValues": [{"fieldName": "synced", "fieldValue": "true"}]
                },
            },
            "credentials": {"nocoDbApiToken": {"id": "1", "name": "Noco"}},
        })
        wf["connections"]["HTTP Request"] = {
            "main": [[{"node": "Noco Update", "type": "main", "index": 0}]]
        }
        ok, issues = validate_n8n_workflow_heuristic(wf)
        assert ok is False
        assert any("Direction K" in i for i in issues)

    def test_resilience_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("N8N_VALIDATOR_RESILIENCE", "off")
        wf = _base_workflow(
            nodes=[
                {
                    "name": "Schedule Trigger",
                    "type": "n8n-nodes-base.scheduleTrigger",
                    "typeVersion": 1.2,
                    "position": [0, 0],
                    "parameters": {
                        "rule": {"interval": [{"field": "minutes", "minutesInterval": 10}]}
                    },
                },
                self._mutating_http(),
            ],
            connections={
                "Schedule Trigger": {
                    "main": [[{"node": "HTTP Create", "type": "main", "index": 0}]]
                }
            },
        )
        ok, issues = validate_n8n_workflow_heuristic(wf)
        assert ok is True
        assert not any("Direction K" in i for i in issues)
