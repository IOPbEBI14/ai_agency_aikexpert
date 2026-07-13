"""
Pydantic-схемы HTTP API (запросы и ответы).
Отделены от agent schemas (core/schemas.py), чтобы не смешивать
контракты LLM-агентов и REST-эндпоинтов.
"""
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, ConfigDict


# ─── Requests ─────────────────────────────────────────────────────────────────

class HumanReviewRequest(BaseModel):
    """POST /api/agency/human-review"""

    human_prompt: str = Field(..., min_length=1, description="Указания человека")
    task_id: Optional[str] = Field(default=None, description="ID задачи для перезапуска")
    resume: bool = Field(default=True, description="Возобновить оркестратор после review")

    model_config = ConfigDict(str_strip_whitespace=True)


class SaveWorkflowRequest(BaseModel):
    """POST /api/agency/workflows/save"""

    task_id: Optional[str] = Field(default=None, description="task_id developer-задачи")
    project_id: Optional[int] = Field(default=None, description="ID проекта (если не текущий)")
    n8n_json: Optional[Dict[str, Any]] = Field(default=None, description="Готовый n8n JSON")
    filename: Optional[str] = Field(default=None, description="Имя файла без/с .json")


class IncreaseTokensRequest(BaseModel):
    """POST /api/agency/increase-tokens — тело опционально (совместимость)."""

    model_config = ConfigDict(extra="ignore")


# ─── Responses (ключевые; остальное — Dict для гибкости фронта) ───────────────

class AgencyActionResponse(BaseModel):
    status: str
    project: Optional[str] = None
    phase: Optional[str] = None
    tokens_used: Optional[int] = None
    token_budget: Optional[int] = None
    error: Optional[str] = None


class IncreaseTokensResponse(BaseModel):
    status: str
    new_budget: int
    added: int


class HumanReviewResponse(BaseModel):
    status: str
    project_id: Optional[int] = None
    task_id: Optional[str] = None
    pm_comment: str = ""
    updated_description: Optional[str] = None
    resumed: bool = False
    error: Optional[str] = None


class WorkflowFileInfo(BaseModel):
    name: str
    size: int
    modified: str


class WorkflowsListResponse(BaseModel):
    workflows: List[WorkflowFileInfo]


class SaveWorkflowResponse(BaseModel):
    status: str
    filename: str
    path: str
    nodes_count: int


class ProjectListItem(BaseModel):
    id: Optional[int] = None
    project_name: str = ""
    client_name: str = "—"
    status: str = ""
    phase: str = ""
    tokens_used: int = 0
    token_budget: int = 0
    completed_at: str = ""
    updated_at: str = ""
    has_final_report: bool = False
    is_current: bool = False
    goal: str = ""


class ProjectsListResponse(BaseModel):
    projects: List[ProjectListItem]
    current_project_id: Optional[int] = None


class ErrorResponse(BaseModel):
    error: str
