"""Артефакты дашборда: analyst УТП + markdown export."""
from core.api_payloads import (
    _artifact_to_markdown,
    _build_agent_artifact,
    _output_preview_for_agent,
)


SAMPLE_ANALYST = {
    "client_name": "Губанова Елена Сергеевна",
    "current_pain_points": [
        {
            "process": "Ручная обработка сообщений",
            "time_per_day_hours": 3.0,
            "cost_per_month_rub": 45000.0,
        }
    ],
    "proposed_automation": [
        {
            "solution": "Автоответчик + CRM",
            "tools": ["n8n", "Telegram"],
            "time_saved_hours_per_day": 2.0,
            "implementation_complexity": "medium",
        }
    ],
    "roi_calculation": {
        "total_time_saved_hours_per_month": 40.0,
        "cost_saved_per_month_rub": 38500.0,
        "implementation_cost_rub": 60000.0,
        "payback_period_months": 1.6,
    },
    "proposal_structure": [
        "Проблема клиента",
        "Решение и УТП",
        "ROI",
    ],
    "notes": "Проверить экономию времени 70–80%",
}


class TestAnalystArtifact:
    def test_build_usp_artifact(self):
        art = _build_agent_artifact("analyst", SAMPLE_ANALYST)
        assert art is not None
        assert art["kind"] == "usp_proposal"
        assert art["downloadable"] is True
        assert art["roi"]["cost_saved_per_month_rub"] == 38500.0
        assert len(art["pain_points"]) == 1
        assert "УТП" in art["title"]

    def test_preview_uses_roi_when_no_summary(self):
        preview = _output_preview_for_agent("analyst", SAMPLE_ANALYST)
        assert "38500" in preview or "38500.0" in preview
        assert "Губанова" in preview

    def test_markdown_export(self):
        md = _artifact_to_markdown("analyst", SAMPLE_ANALYST)
        assert md.startswith("# УТП")
        assert "38500" in md
        assert "Ручная обработка сообщений" in md
        assert "Структура КП" in md
        assert "Автоответчик" in md

    def test_unknown_agent_no_artifact(self):
        assert _build_agent_artifact("qa", {"feedback": "ok"}) is None
