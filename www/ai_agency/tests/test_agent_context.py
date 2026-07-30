"""Тесты памяти итераций агентов (Direction AG)."""
import json
from pathlib import Path

from core.agent_context import (
    build_retry_prompt_block,
    inject_retry_into_input_data,
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
        block = build_retry_prompt_block(ctx, qa_feedback="fix retry")
        assert "ПАМЯТЬ ИТЕРАЦИЙ" in block
        assert "НЕ ИГНОРИРУЙ" in block
        assert "PREVIOUS_ATTEMPT_ARTIFACT" in block
        assert "Webhook" in block

        injected = inject_retry_into_input_data({"goal": "x"}, ctx)
        assert injected["agent_context"]["attempt_count"] == 1
        assert "previous_attempt" in injected or "previous_attempt_preview" in injected

        items = list_project_contexts(7, base=tmp_path)
        assert items and items[0]["task_id"] == "dev_001"

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
        # При наличии memory старый короткий блок не дублируется как единственный
        assert "Собери workflow" in text
