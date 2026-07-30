# Thin wrapper: канонические скрипты лежат в www/ai_agency/
"""Запуск: предпочтительно www/ai_agency/agency_dev_mcp.py (Linux: /home/rdpuser/www/ai_agency/)."""
from pathlib import Path
import runpy

runpy.run_path(
    str(Path(__file__).resolve().parent / "www" / "ai_agency" / "agency_dev_mcp.py"),
    run_name="__main__",
)
