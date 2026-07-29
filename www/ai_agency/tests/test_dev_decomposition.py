"""Тесты нормализации декомпозиции developer (один workflow — один n8n_json)."""
from core.dev_decomposition import (
    MODE_FULL,
    MODE_PREP,
    build_developer_input_data,
    extract_workflow_units,
    normalize_developer_subtasks,
    strip_n8n_from_spec_response,
)


class TestNormalizeDeveloperSubtasks:
    def test_feature_slices_collapse_with_blueprint(self):
        """Типичный баг: retry / ошибки / журнал → один full_workflow."""
        raw = [
            {
                "subtask_id": "dev_002",
                "description": "Переработать workflow: Loop Over Items и backoff",
                "depends_on": [],
                "context": "retry",
            },
            {
                "subtask_id": "dev_003",
                "description": "Реализовать цикл повторов с exponential backoff",
                "depends_on": ["dev_002"],
                "context": "backoff",
            },
            {
                "subtask_id": "dev_004",
                "description": "Классификация ошибок 503/timeout/token",
                "depends_on": ["dev_003"],
                "context": "errors",
            },
            {
                "subtask_id": "dev_005",
                "description": "Финальный статус и запись в журнал",
                "depends_on": ["dev_004"],
                "context": "log",
            },
        ]
        out = normalize_developer_subtasks(raw, has_blueprint=True)
        assert len(out) == 1
        assert out[0]["artifact_mode"] == MODE_FULL
        assert "backoff" in out[0]["description"].lower() or "dev_003" in out[0]["description"]
        assert "dev_005" in out[0]["description"] or "журнал" in out[0]["description"].lower()

    def test_prep_kept_separate(self):
        raw = [
            {
                "subtask_id": "dev_001",
                "description": "Создать таблицу в Bpium для журнала и переменные окружения",
                "depends_on": [],
                "context": "env",
                "artifact_mode": "prep",
            },
            {
                "subtask_id": "dev_002",
                "description": "Собрать n8n workflow по blueprint",
                "depends_on": ["dev_001"],
                "context": "wf",
                "artifact_mode": "full_workflow",
            },
        ]
        out = normalize_developer_subtasks(raw, has_blueprint=True)
        assert len(out) == 2
        modes = {s["subtask_id"]: s["artifact_mode"] for s in out}
        assert modes["dev_001"] == MODE_PREP
        assert modes["dev_002"] == MODE_FULL
        assert out[1]["depends_on"] == ["dev_001"] or "dev_001" in (
            out[1].get("depends_on") or []
        )

    def test_multiple_full_collapsed(self):
        raw = [
            {
                "subtask_id": "dev_a",
                "description": "Workflow A",
                "depends_on": [],
                "artifact_mode": "full_workflow",
            },
            {
                "subtask_id": "dev_b",
                "description": "Workflow B retry",
                "depends_on": ["dev_a"],
                "artifact_mode": "full_workflow",
            },
        ]
        out = normalize_developer_subtasks(raw, has_blueprint=True)
        assert len(out) == 1
        assert out[0]["artifact_mode"] == MODE_FULL

    def test_without_blueprint_keeps_all(self):
        raw = [
            {"subtask_id": "dev_001", "description": "Часть 1", "depends_on": []},
            {"subtask_id": "dev_002", "description": "Часть 2", "depends_on": ["dev_001"]},
        ]
        out = normalize_developer_subtasks(raw, has_blueprint=False)
        assert len(out) == 2
        assert all(s["artifact_mode"] == MODE_FULL for s in out)


class TestBuildDeveloperInputData:
    def test_full_gets_blueprint(self):
        payload = build_developer_input_data(
            subtask={"artifact_mode": MODE_FULL, "context": "c", "assigned_node_names": []},
            architecture_summary="arch",
            blueprint={"workflow_blueprint": {"nodes": []}},
            blueprint_str='{"nodes":[]}',
        )
        assert payload["workflow_blueprint"] == '{"nodes":[]}'
        assert payload["artifact_mode"] == MODE_FULL

    def test_prep_no_blueprint(self):
        payload = build_developer_input_data(
            subtask={"artifact_mode": MODE_PREP, "context": "c", "assigned_node_names": []},
            architecture_summary="arch",
            blueprint={},
            blueprint_str='{"nodes":[1]}',
        )
        assert "workflow_blueprint" not in payload
        assert payload.get("expect_n8n_json") is False


class TestStripN8n:
    def test_strips_workflow_files(self):
        data = strip_n8n_from_spec_response({
            "summary": "prep",
            "n8n_json": {"nodes": [1]},
            "workflow_name": "X",
            "files": [
                {"name": "a.json", "type": "n8n_workflow", "description": "wf"},
                {"name": "env.md", "type": "config", "description": "env"},
            ],
            "setup_instructions": [],
            "testing_steps": [],
        })
        assert data["n8n_json"] is None
        assert data["workflow_name"] is None
        assert all(f["type"] != "n8n_workflow" for f in data["files"])
        assert any(f["name"] == "env.md" for f in data["files"])


class TestExtractWorkflowUnits:
    def test_single_blueprint(self):
        units = extract_workflow_units({
            "workflow_blueprint": {"nodes": [{"name": "A"}], "connections": []},
        })
        assert len(units) == 1
        assert units[0]["workflow_id"] == "wf_1"

    def test_multiple_blueprints(self):
        units = extract_workflow_units({
            "workflow_blueprints": [
                {
                    "workflow_id": "wf_a",
                    "name": "Agent → Manager",
                    "workflow_blueprint": {"nodes": [{"name": "W1"}], "connections": []},
                },
                {
                    "workflow_id": "wf_b",
                    "name": "Manager → Agent",
                    "workflow_blueprint": {"nodes": [{"name": "W2"}], "connections": []},
                },
            ],
        })
        assert len(units) == 2
        assert [u["workflow_id"] for u in units] == ["wf_a", "wf_b"]


class TestMultiWorkflowNormalize:
    def test_two_units_two_full_tasks(self):
        units = [
            {"workflow_id": "wf_a", "name": "A→M", "blueprint": {"nodes": []}},
            {"workflow_id": "wf_b", "name": "M→A", "blueprint": {"nodes": []}},
        ]
        raw = [
            {
                "subtask_id": "dev_001",
                "description": "Сделать оба telegram workflow",
                "artifact_mode": "full_workflow",
            }
        ]
        out = normalize_developer_subtasks(
            raw, has_blueprint=True, workflow_units=units
        )
        fulls = [s for s in out if s["artifact_mode"] == MODE_FULL]
        assert len(fulls) == 2
        assert {s["workflow_id"] for s in fulls} == {"wf_a", "wf_b"}
        assert all("НЕ реализуй другие" in s["description"] for s in fulls)

    def test_unit_blueprint_in_input(self):
        unit = {
            "workflow_id": "wf_a",
            "name": "Only A",
            "blueprint": {"nodes": [{"name": "T"}], "connections": []},
        }
        payload = build_developer_input_data(
            subtask={
                "artifact_mode": MODE_FULL,
                "context": "c",
                "assigned_node_names": [],
                "workflow_id": "wf_a",
                "workflow_name": "Only A",
            },
            architecture_summary="arch",
            blueprint={},
            blueprint_str="{}",
            workflow_unit=unit,
            sibling_workflow_names=["Other"],
        )
        assert payload["workflow_id"] == "wf_a"
        assert payload["one_workflow_per_task"] is True
        assert payload["sibling_workflows_do_not_implement"] == ["Other"]
        assert "wf_a" in payload["workflow_blueprint"]
        assert "Only A" in payload["workflow_blueprint"]
