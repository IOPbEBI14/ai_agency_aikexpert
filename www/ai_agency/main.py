"""
FastAPI HTTP-слой для ИИ-агентства.
Эндпоинты async; синхронный Orchestrator.run() — в фоне через asyncio.to_thread.
OpenAPI: /docs (Swagger), /redoc (ReDoc).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware

from core import api_payloads as payloads
from core.api_schemas import (
    AgencyActionResponse,
    HumanReviewRequest,
    HumanReviewResponse,
    IncreaseTokensResponse,
    SaveWorkflowRequest,
    SaveWorkflowResponse,
    StartProjectRequest,
    WorkflowFileInfo,
    WorkflowsListResponse,
)
from core.config import Config
from core.nocodb import NocoDBClient, ProjectsClient, TasksClient
from core.orchestrator import Orchestrator
from core.utils import call_llm, load_prompt, log_to_agent_logs

_BASE_DIR = Path(__file__).resolve().parent
_WORKFLOWS_DIR = _BASE_DIR / "workflows"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("Main")

db = NocoDBClient()
projects_db = ProjectsClient()
tasks_db = TasksClient()
payloads.set_tasks_db(tasks_db)
orchestrator = Orchestrator()

agency_task: Optional[asyncio.Task] = None

_PROXY_ALLOWED_PATH_RE = re.compile(r"^(\d+)?$")
_PROXY_ALLOWED_PARAMS = frozenset({"limit", "offset", "where", "sort"})
_PROXY_ALLOWED_METHODS = frozenset({"GET", "POST", "PATCH"})
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "content-encoding",
        "content-length",
    }
)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Логирует метод, путь и длительность каждого HTTP-запроса."""

    async def dispatch(self, request: Request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "%s %s → %s (%.1f ms)",
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
        return response


app = FastAPI(
    title="AI Agency OS",
    description="HTTP API оркестратора ИИ-агентства (n8n / e-commerce automation)",
    version="3.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestLoggingMiddleware)


def _safe_filename(name: str) -> str:
    name = (name or "workflow").strip().replace(" ", "_")
    name = re.sub(r"[^\w.\-а-яА-ЯёЁ]+", "", name, flags=re.UNICODE)
    return name[:120] or "workflow"


def _idle_status_payload() -> Dict[str, Any]:
    return {
        "running": orchestrator.agency_running,
        "tokens_used": 0,
        "token_budget": Config.TOKEN_BUDGET,
        "status": "idle",
        "client": Config.DEFAULT_CLIENT_NAME,
        "project_id": None,
        "project_name": "",
        "phase": "",
        "final_report": "",
        "metrics": {},
        "completed_at": "",
        "goal": "",
        "tasks": [],
        "review_tasks": [],
        "pm": {
            "project_name": "",
            "phase": "",
            "status": "idle",
            "client": Config.DEFAULT_CLIENT_NAME,
            "tokens_used": 0,
            "token_budget": Config.TOKEN_BUDGET,
            "reasoning": "",
            "goal": "",
            "active_agents": [],
            "tasks_total": 0,
            "tasks_completed": 0,
            "tasks_failed": 0,
            "review_count": 0,
        },
    }


def _agency_task_running() -> bool:
    return agency_task is not None and not agency_task.done()


def _start_orchestrator_background() -> None:
    """Запускает синхронный orchestrator.run() в thread pool через asyncio.Task."""
    global agency_task
    if _agency_task_running():
        return
    orchestrator.agency_running = True
    agency_task = asyncio.create_task(asyncio.to_thread(orchestrator.run))


@app.get("/")
async def serve_dashboard():
    """Отдаёт дашборд агентства."""
    index = _BASE_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="index.html не найден")
    return FileResponse(index)


@app.get("/api/agency/status")
async def get_status():
    """Статус агентства + задачи текущего проекта."""
    current_project = orchestrator.current_project
    if current_project:
        view = await asyncio.to_thread(payloads._project_view_payload, current_project)
        view["running"] = orchestrator.agency_running
        return view
    return _idle_status_payload()


@app.get("/api/agency/projects")
async def list_projects(limit: int = Query(default=50, ge=1, le=100)):
    """Список проектов для просмотра истории запусков."""
    projects = await asyncio.to_thread(projects_db.list_projects, limit)
    current_id = None
    if orchestrator.current_project:
        current_id = orchestrator.current_project.get("Id")

    items = []
    for p in projects:
        pid = p.get("Id")
        items.append({
            "id": pid,
            "project_name": p.get("project_name") or f"project-{pid}",
            "client_name": p.get("client_name") or "—",
            "status": p.get("status") or "",
            "phase": p.get("current_phase") or "",
            "tokens_used": p.get("tokens_used") or 0,
            "token_budget": p.get("token_budget") or 0,
            "completed_at": p.get("completed_at") or "",
            "updated_at": p.get("updated_at") or p.get("UpdatedAt") or p.get("updateTime") or "",
            "has_final_report": bool((p.get("final_report") or "").strip()),
            "is_current": pid is not None and pid == current_id,
            "goal": (p.get("goal") or "")[:200],
        })
    return {"projects": items, "current_project_id": current_id}


@app.get("/api/agency/projects/{project_id}")
async def get_project(project_id: int):
    """Детали выбранного проекта: задачи, PM, итоговый отчёт."""
    project = await asyncio.to_thread(projects_db.find_project_by_id, project_id)
    if not project:
        raise HTTPException(status_code=404, detail=f"Проект {project_id} не найден")
    view = await asyncio.to_thread(payloads._project_view_payload, project)
    current_id = None
    if orchestrator.current_project:
        current_id = orchestrator.current_project.get("Id")
    view["running"] = False
    view["is_current"] = project_id == current_id
    view["view_mode"] = "history"
    return view


@app.get("/api/agency/artifacts/download")
async def download_artifact(
    project_id: int = Query(..., description="ID проекта"),
    task_id: str = Query(..., min_length=1, description="Логический task_id"),
):
    """Скачать markdown-артефакт задачи (architect / tech_writer / crm_customizer)."""
    tasks = await asyncio.to_thread(tasks_db.get_tasks_by_project, project_id)
    task = next((t for t in tasks if t.get("task_id") == task_id), None)
    if not task:
        raise HTTPException(status_code=404, detail=f"Задача {task_id} не найдена")

    agent_name = task.get("agent_name") or ""
    if agent_name not in ("architect", "tech_writer", "crm_customizer"):
        raise HTTPException(
            status_code=400,
            detail=f"Скачивание не поддерживается для агента {agent_name}",
        )

    parsed = payloads._try_parse_json(task.get("output_data"))
    if not parsed:
        raise HTTPException(status_code=400, detail="У задачи нет валидного output_data")

    markdown = payloads._artifact_to_markdown(agent_name, parsed)
    names = {
        "architect": "architecture",
        "tech_writer": "tech_writer_docs",
        "crm_customizer": "crm_setup_guide",
    }
    filename = f"{names.get(agent_name, agent_name)}_{task_id}.md"
    return Response(
        content=markdown.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/agency/start", response_model=AgencyActionResponse)
async def start_agency(body: Optional[StartProjectRequest] = None):
    """Запуск агентства.

    С телом StartProjectRequest — создаёт новый проект с полями формы и запускает
    (если цикл уже идёт — сначала stop, затем новый проект).
    Без тела — прежнее поведение (resume активного / create из Config defaults).
    """
    global agency_task

    create_payload = None
    if body is not None:
        create_payload = {
            "project_name": body.project_name,
            "client_name": body.client_name,
            "goal": body.goal,
            "token_budget": body.token_budget or Config.TOKEN_BUDGET,
            "current_phase": body.current_phase or "lead_gen",
        }

    if orchestrator.agency_running or _agency_task_running():
        if create_payload is None:
            raise HTTPException(status_code=400, detail="Already running")
        await asyncio.to_thread(orchestrator.stop)
        if agency_task is not None and not agency_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(agency_task), timeout=45.0)
            except asyncio.TimeoutError as e:
                raise HTTPException(
                    status_code=409,
                    detail="Previous run still stopping; retry in a few seconds",
                ) from e
            except Exception:
                pass
            agency_task = None

    ok = await asyncio.to_thread(orchestrator.initialize, create=create_payload)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to initialize project")

    _start_orchestrator_background()
    return AgencyActionResponse(
        status="started",
        project=orchestrator.current_project.get("project_name"),
        project_id=orchestrator.current_project.get("Id"),
        phase=orchestrator.current_project.get("current_phase"),
        tokens_used=orchestrator.current_project.get("tokens_used", 0),
        token_budget=orchestrator.current_project.get("token_budget", Config.TOKEN_BUDGET),
    )


@app.post("/api/agency/stop", response_model=AgencyActionResponse)
async def stop_agency():
    """Остановка оркестратора."""
    await asyncio.to_thread(orchestrator.stop)
    return AgencyActionResponse(status="stopped")


@app.post("/api/agency/resume", response_model=AgencyActionResponse)
async def resume_agency():
    """Возобновить работу агентства."""
    if orchestrator.agency_running or _agency_task_running():
        raise HTTPException(status_code=400, detail="Already running")

    ok = await asyncio.to_thread(orchestrator.initialize)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to initialize project")

    if orchestrator.current_project.get("status") == "needs_human_review":
        project_id = orchestrator.current_project.get("Id")
        if project_id:
            await asyncio.to_thread(
                projects_db.update_project, project_id, {"status": "in_progress"}
            )
        orchestrator.current_project["status"] = "in_progress"

    _start_orchestrator_background()
    return AgencyActionResponse(
        status="resumed",
        phase=orchestrator.current_project.get("current_phase"),
    )


@app.post("/api/agency/increase-tokens", response_model=IncreaseTokensResponse)
async def increase_tokens():
    """Увеличить бюджет токенов."""
    if not orchestrator.current_project:
        await asyncio.to_thread(orchestrator.initialize)

    initial_budget = Config.TOKEN_BUDGET
    current_budget = orchestrator.current_project.get("token_budget", 0) or 0
    new_budget = current_budget + initial_budget

    if orchestrator.current_project.get("Id"):
        await asyncio.to_thread(
            projects_db.update_project,
            orchestrator.current_project["Id"],
            {"token_budget": new_budget},
        )
    orchestrator.current_project["token_budget"] = new_budget

    logger.info(f"Бюджет увеличен: {current_budget} → {new_budget}")
    return IncreaseTokensResponse(
        status="success", new_budget=new_budget, added=initial_budget
    )


@app.post("/api/agency/human-review", response_model=HumanReviewResponse)
async def human_review(body: HumanReviewRequest):
    """Ручное вмешательство: комментарий к задаче и/или проекту."""
    human_prompt = body.human_prompt.strip()
    task_id = body.task_id
    should_resume = body.resume

    if not orchestrator.current_project:
        raise HTTPException(status_code=400, detail="Нет активного проекта")

    project_id = orchestrator.current_project.get("Id")
    if not project_id:
        raise HTTPException(status_code=400, detail="У проекта нет Id")

    try:
        result = HumanReviewResponse(
            status="success",
            project_id=project_id,
            task_id=task_id,
            pm_comment="",
            updated_description=None,
            resumed=False,
        )

        if task_id:
            tasks = await asyncio.to_thread(tasks_db.get_tasks_by_project, project_id)
            task = next((t for t in tasks if t.get("task_id") == task_id), None)
            if not task:
                raise HTTPException(status_code=404, detail=f"Задача {task_id} не найдена")

            pm_prompt = load_prompt("pm")
            pm_task = f"""
ЧЕЛОВЕК ВМЕШАЛСЯ В ЗАДАЧУ {task_id}.

ИСХОДНАЯ ЗАДАЧА:
{task.get('task_description')}

РЕЗУЛЬТАТ АГЕНТА ({task.get('agent_name')}):
{(task.get('output_data') or 'Нет данных')[:2000]}

РЕЗУЛЬТАТ QA / ПРИЧИНА ОСТАНОВКИ:
{(task.get('qa_feedback') or 'Нет данных')[:1000]}

УКАЗАНИЯ ЧЕЛОВЕКА:
{human_prompt}

ТВОЯ ЗАДАЧА:
1. Проанализируй указания человека
2. Скорректируй задачу для агента {task.get('agent_name')}
3. Верни обновлённое описание задачи

ФОРМАТ ОТВЕТА (строго JSON):
{{
    "updated_task_description": "новое описание задачи",
    "pm_comment": "комментарий"
}}

Верни ТОЛЬКО валидный JSON.
"""
            pm_response, pm_tokens = await asyncio.to_thread(
                call_llm, "PM", pm_prompt, pm_task
            )
            orchestrator.current_project["tokens_used"] = (
                orchestrator.current_project.get("tokens_used", 0) or 0
            ) + pm_tokens
            await asyncio.to_thread(
                projects_db.update_project,
                project_id,
                {"tokens_used": orchestrator.current_project["tokens_used"]},
            )

            pm_decision = payloads._try_parse_json(pm_response) or {}
            updated_description = pm_decision.get(
                "updated_task_description", task.get("task_description")
            )
            pm_comment = pm_decision.get("pm_comment", "")

            task_db_id = task.get("Id")
            if task_db_id:
                await asyncio.to_thread(
                    tasks_db.update_task,
                    task_db_id,
                    {
                        "status": "pending",
                        "iteration_count": 0,
                        "qa_approved": "pending",
                        "qa_feedback": f"Human review: {human_prompt[:400]}",
                        "task_description": updated_description,
                        "output_data": None,
                    },
                )

            await asyncio.to_thread(
                log_to_agent_logs,
                project_id=project_id,
                agent_name="PM",
                status="completed",
                task_description=f"Human review для задачи {task_id}: {pm_comment}",
                full_response=json.dumps(
                    {"human_prompt": human_prompt, "pm_decision": pm_decision},
                    ensure_ascii=False,
                ),
                tokens_used=pm_tokens,
            )
            result.pm_comment = pm_comment
            result.updated_description = updated_description
            logger.info(f"Human review для задачи {task_id}: {pm_comment}")
        else:
            await asyncio.to_thread(
                log_to_agent_logs,
                project_id=project_id,
                agent_name="PM",
                status="completed",
                task_description=f"Human review (проект): {human_prompt[:200]}",
                full_response=json.dumps(
                    {"human_prompt": human_prompt}, ensure_ascii=False
                ),
                tokens_used=0,
            )
            result.pm_comment = "Комментарий принят, проект возвращается в работу"

        await asyncio.to_thread(
            projects_db.update_project, project_id, {"status": "in_progress"}
        )
        orchestrator.current_project["status"] = "in_progress"

        if should_resume and not orchestrator.agency_running and not _agency_task_running():
            _start_orchestrator_background()
            result.resumed = True

        return result

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка human review: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/agency/workflows", response_model=WorkflowsListResponse)
async def list_workflows():
    """Список сохранённых n8n workflow файлов."""
    _WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
    files = []
    for path in sorted(
        _WORKFLOWS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    ):
        files.append(
            WorkflowFileInfo(
                name=path.name,
                size=path.stat().st_size,
                modified=datetime.fromtimestamp(path.stat().st_mtime).isoformat(),
            )
        )
    return WorkflowsListResponse(workflows=files)


@app.post("/api/agency/workflows/save", response_model=SaveWorkflowResponse)
async def save_workflow(body: SaveWorkflowRequest):
    """Сохраняет n8n workflow из задачи developer или из переданного JSON."""
    task_id = body.task_id
    n8n_json = body.n8n_json
    filename = body.filename

    try:
        if task_id:
            project_id = body.project_id
            if not project_id:
                if not orchestrator.current_project:
                    raise HTTPException(status_code=400, detail="Нет активного проекта")
                project_id = orchestrator.current_project.get("Id")
            tasks = await asyncio.to_thread(tasks_db.get_tasks_by_project, project_id)
            task = next((t for t in tasks if t.get("task_id") == task_id), None)
            if not task:
                raise HTTPException(
                    status_code=404,
                    detail=f"Задача {task_id} не найдена в проекте {project_id}",
                )
            parsed = payloads._try_parse_json(task.get("output_data"))
            if not parsed:
                raise HTTPException(
                    status_code=400,
                    detail="output_data задачи не является валидным JSON",
                )
            n8n_json = parsed.get("n8n_json")
            if not isinstance(n8n_json, dict) or not n8n_json.get("nodes"):
                raise HTTPException(
                    status_code=400, detail="В output_data нет n8n_json с nodes"
                )
            filename = filename or (
                parsed.get("workflow_name") or n8n_json.get("name") or task_id
            )

        if not isinstance(n8n_json, dict) or not n8n_json.get("nodes"):
            raise HTTPException(
                status_code=400,
                detail="n8n_json обязателен и должен содержать nodes",
            )

        safe_name = _safe_filename(filename or n8n_json.get("name") or "workflow")
        if not safe_name.endswith(".json"):
            safe_name += ".json"

        _WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)
        out_path = _WORKFLOWS_DIR / safe_name
        out_path.write_text(
            json.dumps(n8n_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info(f"Workflow сохранён: {out_path}")
        return SaveWorkflowResponse(
            status="success",
            filename=safe_name,
            path=str(out_path.relative_to(_BASE_DIR)),
            nodes_count=len(n8n_json.get("nodes") or []),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Ошибка сохранения workflow: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/api/agency/workflows/{filename:path}")
async def download_workflow(filename: str):
    """Скачать сохранённый workflow."""
    safe = _safe_filename(filename)
    if not safe.endswith(".json"):
        safe += ".json"
    path = _WORKFLOWS_DIR / safe
    if not path.exists():
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(path, filename=safe, media_type="application/json")


@app.api_route("/api/nocodb", methods=["GET", "POST", "PATCH"], name="nocodb_proxy_root")
@app.api_route("/api/nocodb/{path:path}", methods=["GET", "POST", "PATCH"], name="nocodb_proxy_path")
async def nocodb_proxy(request: Request, path: str = ""):
    """Read/write прокси для NocoDB API v3 (только agent_logs)."""
    if not _PROXY_ALLOWED_PATH_RE.match(path):
        logger.warning(f"Прокси: отклонён путь '{path}'")
        raise HTTPException(status_code=403, detail="Forbidden path")

    if request.method not in _PROXY_ALLOWED_METHODS:
        raise HTTPException(status_code=405, detail="Method not allowed")

    nocodb_url = Config.get_nocodb_records_url()
    if path:
        nocodb_url = f"{nocodb_url}/{path}"

    filtered_params = {
        k: v
        for k, v in request.query_params.items()
        if k in _PROXY_ALLOWED_PARAMS
    }
    if filtered_params:
        query_string = "&".join(f"{k}={v}" for k, v in filtered_params.items())
        nocodb_url += f"?{query_string}"

    headers = {"xc-token": Config.NOCODB_API_TOKEN, "Content-Type": "application/json"}

    body = None
    if request.method in ("POST", "PATCH"):
        try:
            body = await request.json()
        except Exception:
            body = None

    try:
        resp = await asyncio.to_thread(
            requests.request,
            method=request.method,
            url=nocodb_url,
            headers=headers,
            json=body,
            timeout=30,
        )
        out_headers = {
            k: v
            for k, v in resp.headers.items()
            if k.lower() not in _HOP_BY_HOP
        }
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=out_headers,
        )
    except requests.exceptions.RequestException as e:
        logger.error(f"Ошибка прокси в NocoDB: {e}")
        raise HTTPException(status_code=502, detail=str(e)) from e


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """Сохраняем контракт фронта: {"error": "..."} вместо {"detail": "..."}."""
    detail = exc.detail
    if isinstance(detail, list):
        msg = "; ".join(str(x) for x in detail)
    else:
        msg = str(detail)
    return JSONResponse(status_code=exc.status_code, content={"error": msg})


def test_llm_connection():
    """Проверка подключения к Yandex AI Studio."""
    logger.info("Проверка подключения к Yandex AI Studio...")
    logger.info(f"   URL: {Config.get_llm_responses_url()}")
    logger.info(f"   Model: {Config.get_llm_model_uri()}")
    api_key_preview = (
        f"{'*' * 8}{Config.LLM_API_KEY[-4:]}"
        if Config.LLM_API_KEY and len(Config.LLM_API_KEY) > 4
        else "NOT SET"
    )
    logger.info(f"   API Key: {api_key_preview}")

    if not Config.LLM_API_KEY or not Config.LLM_FOLDER_ID:
        logger.error("Не указан LLM_API_KEY или LLM_FOLDER_ID в .env")
        return False

    try:
        content, tokens = call_llm("TEST", "Ты тестовый агент.", "Скажи 'OK' одним словом.")
        logger.info(f"LLM подключен! Ответ: {content[:50]}")
        return True
    except Exception as e:
        logger.error(f"LLM недоступен: {e}")
        return False


if __name__ == "__main__":
    import uvicorn

    logger.info("=" * 60)
    logger.info("FastAPI запускается на порту 5000 (uvicorn)...")
    logger.info("   Swagger UI: http://0.0.0.0:5000/docs")
    logger.info("=" * 60)

    test_llm_connection()
    _WORKFLOWS_DIR.mkdir(parents=True, exist_ok=True)

    uvicorn.run("main:app", host="0.0.0.0", port=5000, reload=False)
