"""Тесты памяти итераций агентов (Direction AG / AI)."""
from pathlib import Path

from core.agent_context import (
    build_retry_prompt_block,
    build_system_memory_hint,
    get_agent_history,
    inject_retry_into_input_data,
    list_agents_with_context,
    list_project_contexts,
    load_task_context,
    record_attempt,
)
from core.utils import build_agent_task


class TestAgentContextStore:
    def test_record_and_retry_block(self, tmp_path: Path):
        record_attempt(
            project_id=7,
            task_id="dev_001",
            agent_name="developer",
            iteration=1,
            status="rejected",
            feedback="Direction K: нет retryOnFail",
            issues=["[HTTP] Direction K: нет retryOnFail"],
            artifact={
                "summary": "wf v1",
                "workflow_name": "Demo",
                "n8n_json": {
                    "nodes": [
                        {"name": "Webhook", "type": "n8n-nodes-base.webhook"},
                        {"name": "HTTP", "type": "n8n-nodes-base.httpRequest"},
                    ],
                    "connections": {},
                },
                "files": [],
                "setup_instructions": [],
                "testing_steps": [],
            },
            base=tmp_path,
        )
        ctx = load_task_context(7, "dev_001", base=tmp_path)
        assert len(ctx["attempts"]) == 1
        assert ctx["open_issues"]
        block = build_retry_prompt_block(
            ctx, qa_feedback="fix retry", agent_name="developer"
        )
        assert "ПАМЯТЬ ИТЕРАЦИЙ" in block
        assert "НЕ ИГНОРИРУЙ" in block
        assert "PREVIOUS_ATTEMPT_ARTIFACT" in block
        assert "Webhook" in block

        injected = inject_retry_into_input_data(
            {"goal": "x"}, ctx, agent_name="developer"
        )
        assert injected["agent_context"]["attempt_count"] == 1
        assert injected["agent_context"]["agent_name"] == "developer"
        assert "previous_attempt" in injected or "previous_attempt_preview" in injected

        items = list_project_contexts(7, base=tmp_path)
        assert items and items[0]["task_id"] == "dev_001"
        assert "developer" in (items[0].get("agents") or [])

    def test_any_agent_history(self, tmp_path: Path):
        record_attempt(
            project_id=3,
            task_id="task_sales",
            agent_name="sales",
            iteration=1,
            status="rejected",
            feedback="пустые messages",
            issues=["messages=[]"],
            artifact={"summary": "draft", "messages": []},
            base=tmp_path,
        )
        record_attempt(
            project_id=3,
            task_id="task_arch",
            agent_name="architect",
            iteration=2,
            status="approved",
            feedback="ok",
            artifact={"summary": "arch v2"},
            base=tmp_path,
        )
        agents = list_agents_with_context(3, base=tmp_path)
        names = {a["agent_name"] for a in agents}
        assert names >= {"sales", "architect"}

        sales_hist = get_agent_history(3, "sales", base=tmp_path)
        assert len(sales_hist) == 1
        assert sales_hist[0]["task_id"] == "task_sales"

        hint = build_system_memory_hint("analyst")
        assert "analyst" in hint

    def test_build_agent_task_uses_memory(self):
        memory = "ПАМЯТЬ ИТЕРАЦИЙ\nPREVIOUS_ATTEMPT_ARTIFACT: {...}"
        text = build_agent_task(
            "Собери workflow",
            {"artifact_mode": "full_workflow"},
            "old feedback",
            2,
            iteration_memory=memory,
        )
        assert "ПАМЯТЬ ИТЕРАЦИЙ" in text
        assert "PREVIOUS_ATTEMPT_ARTIFACT" in text
        assert "Собери workflow" in text
