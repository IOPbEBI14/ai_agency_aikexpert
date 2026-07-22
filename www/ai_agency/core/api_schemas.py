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


class StartProjectRequest(BaseModel):
    """POST /api/agency/start — создание проекта и запуск оркестратора.

    Если тело передано — создаётся НОВЫЙ проект с указанными полями.
    Пустое тело (обратная совместимость) — resume/create из дефолтов Config.
    """

    project_name: str = Field(..., min_length=1, max_length=200, description="Название проекта")
    client_name: str = Field(..., min_length=1, max_length=200, description="Клиент / компания")
    goal: str = Field(
        ...,
        min_length=10,
        max_length=20000,
        description="Цель проекта / ТЗ (что автоматизировать, для кого, ограничения)",
    )
    token_budget: Optional[int] = Field(
        default=None,
        ge=1000,
        le=10_000_000,
        description="Лимит токенов (по умолчанию из Config.TOKEN_BUDGET)",
    )
    current_phase: Optional[str] = Field(
        default="lead_gen",
        description="Стартовая фаза: lead_gen | sales | analysis | …",
    )

    model_config = ConfigDict(str_strip_whitespace=True)


class IncreaseTokensRequest(BaseModel):
    """POST /api/agency/increase-tokens — тело опционально (совместимость)."""

    model_config = ConfigDict(extra="ignore")


class SetLlmProviderRequest(BaseModel):
    """POST /api/agency/llm/provider — переключение активного LLM."""

    provider: str = Field(
        ...,
        min_length=1,
        description="openai | grok | anthropic | deepseek | yandexgpt | gigachat",
    )

    model_config = ConfigDict(str_strip_whitespace=True)


class RefineProjectRequest(BaseModel):
    """POST /api/agency/refine — новая итерация по замечаниям человека.

    Работает для текущего и любого проекта из истории (project_id).
    """

    human_prompt: str = Field(
        ...,
        min_length=5,
        max_length=20000,
        description="Замечания / что доработать относительно предыдущего запуска",
    )
    project_id: Optional[int] = Field(
        default=None,
        description="Id проекта (если не указан — текущий в оркестраторе)",
    )
    resume: bool = Field(
        default=True,
        description="Сразу запустить оркестратор после построения графа итерации",
    )

    model_config = ConfigDict(str_strip_whitespace=True)


# ─── Responses (ключевые; остальное — Dict для гибкости фронта) ───────────────

class AgencyActionResponse(BaseModel):
    status: str
    project: Optional[str] = None
    project_id: Optional[int] = None
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


class RefineProjectResponse(BaseModel):
    status: str
    project_id: Optional[int] = None
    iteration: int = 1
    tasks_created: int = 0
    pm_comment: str = ""
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
