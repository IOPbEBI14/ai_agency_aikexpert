"""Память итераций агентов (Direction AG).

Проблема: на retry developer получает только qa_feedback, без предыдущего
артефакта → каждый раз генерирует workflow «с нуля» и повторяет те же ошибки.

Решение:
  - JSON-store по (project_id, task_id) с историей попыток;
  - inject предыдущего артефакта + open issues в prompt;
  - тот же store отдаёт MCP-сервер agency_agent_context_mcp.py.
"""
from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("AgentContext")

_LOCK = threading.RLock()

DEFAULT_STORE_DIR = Path(__file__).resolve().parent.parent / "data" / "agent_context"
# Лимит превью артефакта в store/prompt (символы)
ARTIFACT_PREVIEW_CHARS = 14000
PROMPT_CONTEXT_CHARS = 16000


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def store_dir(base: Optional[Path] = None) -> Path:
    root = Path(base) if base else DEFAULT_STORE_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_id(value: Any) -> str:
    text = str(value or "unknown").strip() or "unknown"
    return re.sub(r"[^\w.\-]+", "_", text)[:180]


def context_path(
    project_id: Any,
    task_id: str,
    *,
    base: Optional[Path] = None,
) -> Path:
    return store_dir(base) / _safe_id(project_id) / f"{_safe_id(task_id)}.json"


def _empty_context(project_id: Any, task_id: str) -> Dict[str, Any]:
    return {
        "project_id": project_id,
        "task_id": task_id,
        "updated_at": _now_iso(),
        "attempts": [],
        "open_issues": [],
        "last_status": None,
    }


def load_task_context(
    project_id: Any,
    task_id: str,
    *,
    base: Optional[Path] = None,
) -> Dict[str, Any]:
    path = context_path(project_id, task_id, base=base)
    with _LOCK:
        if not path.exists():
            return _empty_context(project_id, task_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return _empty_context(project_id, task_id)
            data.setdefault("attempts", [])
            data.setdefault("open_issues", [])
            return data
        except Exception as e:
            logger.warning("⚠️ Не удалось прочитать agent context %s: %s", path, e)
            return _empty_context(project_id, task_id)


def save_task_context(
    ctx: Dict[str, Any],
    *,
    base: Optional[Path] = None,
) -> Path:
    project_id = ctx.get("project_id")
    task_id = str(ctx.get("task_id") or "")
    path = context_path(project_id, task_id, base=base)
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        ctx["updated_at"] = _now_iso()
        path.write_text(
            json.dumps(ctx, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    return path


def digest_artifact(artifact: Any) -> Dict[str, Any]:
    """Короткий дайджест ответа агента / n8n_json для истории."""
    if artifact is None:
        return {}
    data = artifact
    if isinstance(artifact, str):
        try:
            data = json.loads(artifact)
        except json.JSONDecodeError:
            return {"preview": artifact[:2000], "chars": len(artifact)}
    if not isinstance(data, dict):
        return {"type": type(data).__name__}

    n8n = data.get("n8n_json") if isinstance(data.get("n8n_json"), dict) else None
    if n8n is None and isinstance(data.get("nodes"), list):
        n8n = data

    digest: Dict[str, Any] = {
        "summary": str(data.get("summary") or "")[:500],
        "workflow_name": data.get("workflow_name"),
    }
    if isinstance(n8n, dict):
        nodes = n8n.get("nodes") if isinstance(n8n.get("nodes"), list) else []
        names = []
        types = []
        for n in nodes:
            if not isinstance(n, dict):
                continue
            names.append(str(n.get("name") or ""))
            types.append(str(n.get("type") or "").split(".")[-1])
        digest.update({
            "node_count": len(nodes),
            "node_names": names[:40],
            "node_types": types[:40],
            "has_connections": bool(n8n.get("connections")),
        })
    return digest


def _artifact_preview(artifact: Any, limit: int = ARTIFACT_PREVIEW_CHARS) -> str:
    if artifact is None:
        return ""
    if isinstance(artifact, (dict, list)):
        text = json.dumps(artifact, ensure_ascii=False, indent=2, default=str)
    else:
        text = str(artifact)
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… [truncated {len(text) - limit} chars]"


def record_attempt(
    *,
    project_id: Any,
    task_id: str,
    agent_name: str,
    iteration: int,
    status: str,
    feedback: str = "",
    issues: Optional[List[str]] = None,
    artifact: Any = None,
    notes: str = "",
    base: Optional[Path] = None,
) -> Dict[str, Any]:
    """Добавляет попытку в историю задачи и обновляет open_issues."""
    ctx = load_task_context(project_id, task_id, base=base)
    issue_list = [str(i) for i in (issues or []) if i]
    entry = {
        "iteration": int(iteration or 0),
        "agent_name": agent_name,
        "status": status,
        "ts": _now_iso(),
        "feedback": (feedback or "")[:8000],
        "issues": issue_list[:50],
        "digest": digest_artifact(artifact),
        "artifact_preview": _artifact_preview(artifact),
        "notes": (notes or "")[:1000],
    }
    ctx["attempts"].append(entry)
    # Не раздуваем бесконечно
    if len(ctx["attempts"]) > 30:
        ctx["attempts"] = ctx["attempts"][-30:]
    ctx["last_status"] = status
    if status in ("rejected", "error", "failed"):
        ctx["open_issues"] = issue_list or (
            [feedback[:500]] if feedback else ctx.get("open_issues") or []
        )
    elif status in ("approved", "completed"):
        ctx["open_issues"] = []
    save_task_context(ctx, base=base)
    logger.info(
        "🧠 AgentContext %s/%s iter=%s status=%s issues=%s",
        project_id, task_id, iteration, status, len(issue_list),
    )
    return ctx


def last_attempt(ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    attempts = ctx.get("attempts") or []
    return attempts[-1] if attempts else None


def build_retry_prompt_block(
    ctx: Dict[str, Any],
    *,
    qa_feedback: str = "",
    max_chars: int = PROMPT_CONTEXT_CHARS,
) -> str:
    """Текст для user-prompt: не начинай с нуля, патчь предыдущую попытку."""
    attempts = ctx.get("attempts") or []
    if not attempts and not qa_feedback:
        return ""

    lines: List[str] = [
        "═══════════════════════════════════════════════════════════",
        "ПАМЯТЬ ИТЕРАЦИЙ (Agent Context) — НЕ ИГНОРИРУЙ",
        "═══════════════════════════════════════════════════════════",
        "Ты УЖЕ делал попытки по этой задаче. ЗАПРЕЩЕНО генерировать результат",
        "«с нуля», игнорируя предыдущий артефакт. Возьми previous_attempt /",
        "artifact_preview как базу и ИСПРАВЬ только open_issues / qa_feedback.",
        "",
    ]
    lines.append(f"Всего попыток в истории: {len(attempts)}")
    # Краткие итоги последних попыток
    for a in attempts[-4:]:
        dig = a.get("digest") or {}
        lines.append(
            f"- iter {a.get('iteration')}: status={a.get('status')}; "
            f"nodes={dig.get('node_count')}; "
            f"wf={dig.get('workflow_name') or '-'}; "
            f"issues={len(a.get('issues') or [])}"
        )
        for iss in (a.get("issues") or [])[:5]:
            lines.append(f"    • {iss[:240]}")

    open_issues = ctx.get("open_issues") or []
    if open_issues:
        lines.append("\nОТКРЫТЫЕ ЗАМЕЧАНИЯ (обязательно закрыть в этом ответе):")
        for iss in open_issues[:25]:
            lines.append(f"- {iss}")

    if qa_feedback:
        lines.append("\nТЕКУЩИЙ QA / VALIDATOR FEEDBACK:")
        lines.append(qa_feedback[:6000])

    prev = last_attempt(ctx)
    if prev and prev.get("artifact_preview"):
        lines.append("\nPREVIOUS_ATTEMPT_ARTIFACT (база для патча, не выбрасывай):")
        lines.append(prev["artifact_preview"])

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n… [agent context truncated]"
    return text


def inject_retry_into_input_data(
    input_data: Dict[str, Any],
    ctx: Dict[str, Any],
) -> Dict[str, Any]:
    """Добавляет structured retry-контекст во вход задачи."""
    out = dict(input_data or {})
    attempts = ctx.get("attempts") or []
    if not attempts:
        return out
    prev = last_attempt(ctx) or {}
    out["agent_context"] = {
        "attempt_count": len(attempts),
        "open_issues": ctx.get("open_issues") or [],
        "last_status": ctx.get("last_status"),
        "last_digest": prev.get("digest") or {},
        "instruction": (
            "Исправь previous_attempt по open_issues. Не создавай новый workflow "
            "с нуля, если previous_attempt уже есть."
        ),
    }
    preview = prev.get("artifact_preview") or ""
    if preview:
        # Пытаемся отдать JSON, иначе строку
        try:
            out["previous_attempt"] = json.loads(preview.split("\n… [truncated")[0])
        except json.JSONDecodeError:
            out["previous_attempt_preview"] = preview[:ARTIFACT_PREVIEW_CHARS]
    return out


def list_project_contexts(
    project_id: Any,
    *,
    base: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Список task_id с кратким статусом для MCP."""
    root = store_dir(base) / _safe_id(project_id)
    if not root.exists():
        return []
    items = []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            items.append({
                "task_id": data.get("task_id") or path.stem,
                "attempts": len(data.get("attempts") or []),
                "last_status": data.get("last_status"),
                "open_issues": len(data.get("open_issues") or []),
                "updated_at": data.get("updated_at"),
            })
        except Exception:
            items.append({"task_id": path.stem, "error": "unreadable"})
    return items


def clear_task_context(
    project_id: Any,
    task_id: str,
    *,
    base: Optional[Path] = None,
) -> bool:
    path = context_path(project_id, task_id, base=base)
    with _LOCK:
        if path.exists():
            path.unlink()
            return True
    return False
