"""Сборка payload для дашборда и артефактов."""
import json
import logging
import re
from typing import Any, Dict, Optional

from core.config import Config
from core.utils import try_fix_truncated_json

logger = logging.getLogger("Main")
_tasks_db_ref = None


def set_tasks_db(client) -> None:
    global _tasks_db_ref
    _tasks_db_ref = client


def _tasks_db():
    if _tasks_db_ref is None:
        raise RuntimeError("tasks_db not initialized — call set_tasks_db()")
    return _tasks_db_ref

def _try_parse_json(raw: Any) -> Optional[Dict[str, Any]]:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        fixed = try_fix_truncated_json(text)
        if not fixed:
            return None
        try:
            data = json.loads(fixed)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            return None


def _parse_metrics(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _build_tasks_payload(project_id: Any):
    """Собирает tasks_payload и review_tasks для проекта."""
    tasks_payload = []
    review_tasks = []
    if not project_id:
        return tasks_payload, review_tasks
    try:
        for t in _tasks_db().get_tasks_by_project(project_id):
            agent_name = t.get("agent_name")
            item = {
                "id": t.get("Id"),
                "task_id": t.get("task_id"),
                "agent_name": agent_name,
                "task_description": (t.get("task_description") or "")[:600],
                "status": t.get("status"),
                "qa_approved": t.get("qa_approved"),
                "qa_feedback": (t.get("qa_feedback") or "")[:6000],
                "iteration_count": t.get("iteration_count") or 0,
                "max_iterations": t.get("max_iterations") or 3,
                "tokens_used": t.get("tokens_used") or 0,
                "depends_on": t.get("depends_on") or "[]",
                "updated_at": t.get("updated_at") or "",
                "has_n8n_json": False,
                "workflow_name": None,
                "output_preview": None,
                "artifact": None,
            }
            parsed = _try_parse_json(t.get("output_data"))
            if parsed:
                item["output_preview"] = _output_preview_for_agent(agent_name, parsed)
                n8n = parsed.get("n8n_json")
                if isinstance(n8n, dict) and n8n.get("nodes"):
                    item["has_n8n_json"] = True
                    item["workflow_name"] = (
                        parsed.get("workflow_name")
                        or n8n.get("name")
                        or t.get("task_id")
                    )
                item["artifact"] = _build_agent_artifact(agent_name, parsed)
            tasks_payload.append(item)
            if t.get("status") in ("needs_human_review", "failed"):
                review_tasks.append({
                    "task_id": t.get("task_id"),
                    "agent_name": agent_name,
                    "status": t.get("status"),
                    "qa_feedback": (t.get("qa_feedback") or "")[:500],
                })
    except Exception as e:
        logger.warning(f"⚠️ Не удалось загрузить задачи проекта {project_id}: {e}")
    return tasks_payload, review_tasks


def _output_preview_for_agent(agent_name: Optional[str], parsed: Dict[str, Any]) -> str:
    """Короткий текст для карточки задачи (analyst без summary — из ROI/клиента)."""
    preview = (
        parsed.get("summary")
        or parsed.get("pm_comment")
        or parsed.get("approach")
        or ""
    )
    if preview:
        return str(preview)[:1500]

    if agent_name == "analyst":
        client = parsed.get("client_name") or "клиент"
        roi = parsed.get("roi_calculation") if isinstance(parsed.get("roi_calculation"), dict) else {}
        saved = roi.get("cost_saved_per_month_rub")
        parts = [f"УТП / КП для {client}"]
        if saved is not None:
            parts.append(f"ROI рассчитан: экономия {saved} руб/мес")
        pains = parsed.get("current_pain_points") or []
        if isinstance(pains, list) and pains:
            first = pains[0] if isinstance(pains[0], dict) else None
            if first and first.get("process"):
                parts.append(f"Боль: {first['process']}")
        return ". ".join(parts)[:1500]

    if agent_name == "qa":
        feedback = parsed.get("feedback") or parsed.get("qa_feedback") or ""
        score = parsed.get("score")
        if score is not None:
            return f"QA score={score}. {feedback}"[:1500]
        return str(feedback)[:1500]

    return ""


def _build_agent_artifact(agent_name: Optional[str], parsed: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Структурированный артефакт для карточек агентов (скачивание .md)."""
    if not agent_name or not parsed:
        return None

    if agent_name == "analyst":
        pains = []
        for p in parsed.get("current_pain_points") or []:
            if isinstance(p, dict):
                pains.append({
                    "process": p.get("process") or "",
                    "time_per_day_hours": p.get("time_per_day_hours"),
                    "cost_per_month_rub": p.get("cost_per_month_rub"),
                })
        autos = []
        for a in parsed.get("proposed_automation") or []:
            if isinstance(a, dict):
                autos.append({
                    "solution": a.get("solution") or "",
                    "tools": a.get("tools") or [],
                    "time_saved_hours_per_day": a.get("time_saved_hours_per_day"),
                    "implementation_complexity": a.get("implementation_complexity") or "",
                })
        roi = parsed.get("roi_calculation") if isinstance(parsed.get("roi_calculation"), dict) else {}
        structure = [
            str(s) for s in (parsed.get("proposal_structure") or []) if s is not None
        ]
        return {
            "kind": "usp_proposal",
            "title": f"УТП / КП — {parsed.get('client_name') or 'клиент'}",
            "summary": (
                f"Экономия {roi.get('cost_saved_per_month_rub', '—')} руб/мес, "
                f"окупаемость {roi.get('payback_period_months', '—')} мес"
            ),
            "client_name": parsed.get("client_name") or "",
            "pain_points": pains[:12],
            "proposed_automation": autos[:12],
            "roi": {
                "total_time_saved_hours_per_month": roi.get("total_time_saved_hours_per_month"),
                "cost_saved_per_month_rub": roi.get("cost_saved_per_month_rub"),
                "implementation_cost_rub": roi.get("implementation_cost_rub"),
                "payback_period_months": roi.get("payback_period_months"),
            },
            "proposal_structure": structure[:20],
            "notes": parsed.get("notes") or "",
            "downloadable": True,
            "download_name": "usp_proposal",
        }

    if agent_name == "architect":
        systems = []
        for s in parsed.get("systems") or []:
            if isinstance(s, dict):
                systems.append({
                    "name": s.get("name") or "",
                    "role": s.get("role") or "",
                    "api_available": s.get("api_available"),
                    "limitations": s.get("limitations"),
                })
        data_flow = []
        for step in parsed.get("data_flow") or []:
            if isinstance(step, dict):
                data_flow.append({
                    "step": step.get("step"),
                    "from": step.get("from") or step.get("from_"),
                    "to": step.get("to"),
                    "trigger": step.get("trigger"),
                    "data": step.get("data"),
                    "transformation": step.get("transformation"),
                })
        handoff = parsed.get("handoff_to_developer") or {}
        blueprint = handoff.get("workflow_blueprint") if isinstance(handoff, dict) else None
        nodes = (blueprint or {}).get("nodes") if isinstance(blueprint, dict) else []
        return {
            "kind": "architecture",
            "title": parsed.get("summary") or "Архитектура",
            "summary": parsed.get("summary") or "",
            "approach": parsed.get("approach") or "",
            "systems": systems,
            "data_flow": data_flow,
            "tech_stack": parsed.get("tech_stack") or [],
            "estimated_complexity": parsed.get("estimated_complexity"),
            "estimated_time_hours": parsed.get("estimated_time_hours"),
            "risks": parsed.get("risks") or [],
            "recommendations": parsed.get("recommendations") or "",
            "blueprint_nodes_count": len(nodes) if isinstance(nodes, list) else 0,
            "downloadable": True,
            "download_name": "architecture",
        }

    if agent_name == "tech_writer":
        documents = []
        for doc in parsed.get("documents") or []:
            if not isinstance(doc, dict):
                continue
            sections = doc.get("sections") or []
            documents.append({
                "title": doc.get("title") or "Документ",
                "type": doc.get("type") or "",
                "audience": doc.get("audience") or "",
                "sections_count": len(sections) if isinstance(sections, list) else 0,
                "preview": (
                    (sections[0].get("content") or "")[:280]
                    if isinstance(sections, list) and sections and isinstance(sections[0], dict)
                    else ""
                ),
            })
        return {
            "kind": "documents",
            "title": parsed.get("summary") or "Документация",
            "summary": parsed.get("summary") or "",
            "documents": documents,
            "checklist": parsed.get("checklist") or [],
            "faq": [
                {"question": f.get("question"), "answer": f.get("answer")}
                for f in (parsed.get("faq") or [])
                if isinstance(f, dict)
            ][:20],
            "downloadable": True,
            "download_name": "tech_writer_docs",
        }

    if agent_name == "crm_customizer":
        entities = []
        for e in parsed.get("entities") or []:
            if isinstance(e, dict):
                entities.append(e.get("name") or e.get("entity_name") or str(e))
        pipelines = []
        for p in parsed.get("pipelines") or []:
            if isinstance(p, dict):
                pipelines.append(p.get("name") or p.get("pipeline_name") or str(p))
        return {
            "kind": "crm_setup",
            "title": parsed.get("summary") or "Настройка CRM",
            "summary": parsed.get("summary") or "",
            "platform": parsed.get("platform") or "",
            "setup_steps": parsed.get("setup_steps") or [],
            "entities": entities,
            "pipelines": pipelines,
            "field_mapping_count": len(parsed.get("field_mapping") or []),
            "notes": parsed.get("notes") or "",
            "downloadable": True,
            "download_name": "crm_setup_guide",
        }

    if agent_name == "client_hunter":
        clients = [c for c in (parsed.get("clients") or []) if isinstance(c, dict)]
        with_contacts = sum(
            1 for c in clients if c.get("contact_email") or c.get("contact_phone")
        )
        preview = []
        for c in clients[:8]:
            preview.append({
                "company_name": c.get("company_name") or "",
                "website": c.get("website") or "",
                "decision_maker_role": c.get("decision_maker_role") or "",
                "contact_email": c.get("contact_email") or "",
                "contact_phone": c.get("contact_phone") or "",
            })
        return {
            "kind": "clients",
            "title": parsed.get("summary") or "Найденные клиенты",
            "summary": parsed.get("summary") or "",
            "total_found": parsed.get("total_found") or len(clients),
            "with_contacts": with_contacts,
            "clients_preview": preview,
            "downloadable": True,
            "download_name": "clients_contacts",
        }

    if agent_name == "sales":
        messages = [m for m in (parsed.get("messages") or []) if isinstance(m, dict)]
        return {
            "kind": "outreach",
            "title": "Готовые письма (ручная отправка)",
            "summary": parsed.get("next_steps") or "",
            "messages_count": len(messages),
            "send_mode": parsed.get("send_mode") or "manual_export_only",
            "messages_preview": [
                {
                    "lead_name": m.get("lead_name") or "",
                    "channel": m.get("channel") or "",
                    "to_email": m.get("to_email") or "",
                    "subject": m.get("subject") or "",
                    "preview": (m.get("message_text") or "")[:180],
                }
                for m in messages[:8]
            ],
            "downloadable": True,
            "download_name": "outreach_letters",
            "outreach_export": True,
        }

    return None


def _artifact_to_markdown(agent_name: str, parsed: Dict[str, Any]) -> str:
    """Собирает markdown-файл из output_data агента."""
    lines: list = []

    if agent_name == "analyst":
        client = parsed.get("client_name") or "Клиент"
        lines.append(f"# УТП / коммерческое предложение — {client}")
        lines.append("")
        roi = parsed.get("roi_calculation") if isinstance(parsed.get("roi_calculation"), dict) else {}
        lines.append("## Экономика (ROI)")
        lines.append(
            f"- Экономия времени: {roi.get('total_time_saved_hours_per_month', '—')} ч/мес"
        )
        lines.append(
            f"- Экономия денег: {roi.get('cost_saved_per_month_rub', '—')} руб/мес"
        )
        lines.append(
            f"- Стоимость внедрения: {roi.get('implementation_cost_rub', '—')} руб"
        )
        lines.append(
            f"- Окупаемость: {roi.get('payback_period_months', '—')} мес"
        )
        lines.append("")
        pains = parsed.get("current_pain_points") or []
        if pains:
            lines.append("## Болевые точки")
            for p in pains:
                if not isinstance(p, dict):
                    continue
                lines.append(
                    f"- **{p.get('process') or '—'}**: "
                    f"{p.get('time_per_day_hours', '—')} ч/день, "
                    f"{p.get('cost_per_month_rub', '—')} руб/мес"
                )
            lines.append("")
        autos = parsed.get("proposed_automation") or []
        if autos:
            lines.append("## Предлагаемая автоматизация")
            for a in autos:
                if not isinstance(a, dict):
                    continue
                tools = a.get("tools") or []
                tools_s = ", ".join(str(t) for t in tools) if isinstance(tools, list) else str(tools)
                lines.append(f"### {a.get('solution') or 'Решение'}")
                lines.append(f"- Инструменты: {tools_s or '—'}")
                lines.append(
                    f"- Экономия: {a.get('time_saved_hours_per_day', '—')} ч/день"
                )
                lines.append(
                    f"- Сложность: {a.get('implementation_complexity') or '—'}"
                )
                lines.append("")
        structure = parsed.get("proposal_structure") or []
        if structure:
            lines.append("## Структура КП / УТП")
            for i, slide in enumerate(structure, 1):
                lines.append(f"{i}. {slide}")
            lines.append("")
        if parsed.get("notes"):
            lines.append("## Заметки")
            lines.append(str(parsed["notes"]))
            lines.append("")
        handoff = parsed.get("handoff_to_architect")
        if isinstance(handoff, dict) and handoff:
            lines.append("## Передача архитектору")
            lines.append("```json")
            lines.append(json.dumps(handoff, ensure_ascii=False, indent=2))
            lines.append("```")
            lines.append("")
        return "\n".join(lines).strip() + "\n"

    if agent_name == "architect":
        lines.append(f"# {parsed.get('summary') or 'Архитектура'}")
        lines.append("")
        if parsed.get("approach"):
            lines.append("## Подход")
            lines.append(str(parsed["approach"]))
            lines.append("")
        if parsed.get("tech_stack"):
            lines.append("## Стек")
            for item in parsed["tech_stack"]:
                lines.append(f"- {item}")
            lines.append("")
        if parsed.get("systems"):
            lines.append("## Системы")
            for s in parsed["systems"]:
                if not isinstance(s, dict):
                    continue
                lim = f" — {s.get('limitations')}" if s.get("limitations") else ""
                lines.append(
                    f"- **{s.get('name')}** ({s.get('role')})"
                    f"{', API' if s.get('api_available') else ''}{lim}"
                )
            lines.append("")
        if parsed.get("data_flow"):
            lines.append("## Поток данных")
            for step in parsed["data_flow"]:
                if not isinstance(step, dict):
                    continue
                fr = step.get("from") or step.get("from_")
                lines.append(
                    f"{step.get('step')}. {fr} → {step.get('to')} "
                    f"(триггер: {step.get('trigger')})"
                )
                if step.get("data"):
                    lines.append(f"   - Данные: {step['data']}")
                if step.get("transformation"):
                    lines.append(f"   - Трансформация: {step['transformation']}")
            lines.append("")
        if parsed.get("risks"):
            lines.append("## Риски")
            for r in parsed["risks"]:
                lines.append(f"- {r}")
            lines.append("")
        if parsed.get("recommendations"):
            lines.append("## Рекомендации")
            lines.append(str(parsed["recommendations"]))
            lines.append("")
        handoff = parsed.get("handoff_to_developer")
        if isinstance(handoff, dict) and handoff.get("workflow_blueprint"):
            bp = handoff["workflow_blueprint"]
            lines.append("## Workflow blueprint")
            for node in bp.get("nodes") or []:
                if isinstance(node, dict):
                    lines.append(
                        f"- `{node.get('name')}` ({node.get('type')}): {node.get('purpose') or ''}"
                    )
            lines.append("")

    elif agent_name == "tech_writer":
        lines.append(f"# {parsed.get('summary') or 'Документация'}")
        lines.append("")
        for doc in parsed.get("documents") or []:
            if not isinstance(doc, dict):
                continue
            lines.append(f"## {doc.get('title') or 'Документ'}")
            lines.append(f"*Тип: {doc.get('type') or '—'} · Аудитория: {doc.get('audience') or '—'}*")
            lines.append("")
            for sec in doc.get("sections") or []:
                if not isinstance(sec, dict):
                    continue
                lines.append(f"### {sec.get('title') or 'Раздел'}")
                lines.append(str(sec.get("content") or ""))
                lines.append("")
        if parsed.get("faq"):
            lines.append("## FAQ")
            for item in parsed["faq"]:
                if isinstance(item, dict):
                    lines.append(f"**Q:** {item.get('question')}")
                    lines.append(f"**A:** {item.get('answer')}")
                    lines.append("")
        if parsed.get("checklist"):
            lines.append("## Чек-лист")
            for step in parsed["checklist"]:
                lines.append(f"- [ ] {step}")
            lines.append("")

    elif agent_name == "crm_customizer":
        lines.append(f"# {parsed.get('summary') or 'Настройка CRM'}")
        lines.append("")
        lines.append(f"**Платформа:** {parsed.get('platform') or '—'}")
        lines.append("")
        if parsed.get("setup_steps"):
            lines.append("## Инструкция по настройке")
            for i, step in enumerate(parsed["setup_steps"], 1):
                lines.append(f"{i}. {step}")
            lines.append("")
        if parsed.get("entities"):
            lines.append("## Сущности")
            for e in parsed["entities"]:
                if isinstance(e, dict):
                    lines.append(f"- {e.get('name') or e}")
                else:
                    lines.append(f"- {e}")
            lines.append("")
        if parsed.get("pipelines"):
            lines.append("## Воронки")
            for p in parsed["pipelines"]:
                if isinstance(p, dict):
                    lines.append(f"- {p.get('name') or p}")
                else:
                    lines.append(f"- {p}")
            lines.append("")
        if parsed.get("field_mapping"):
            lines.append("## Маппинг полей")
            for m in parsed["field_mapping"]:
                if isinstance(m, dict):
                    lines.append(
                        f"- {m.get('source') or m.get('from') or '?'} → "
                        f"{m.get('target') or m.get('to') or '?'}"
                    )
            lines.append("")
        if parsed.get("notes"):
            lines.append("## Заметки")
            lines.append(str(parsed["notes"]))
            lines.append("")

    elif agent_name == "client_hunter":
        lines.append(f"# {parsed.get('summary') or 'Клиенты и контакты'}")
        lines.append("")
        for c in parsed.get("clients") or []:
            if not isinstance(c, dict):
                continue
            lines.append(f"## {c.get('company_name') or '—'}")
            lines.append(f"- Сайт: {c.get('website') or '—'}")
            lines.append(f"- Ниша: {c.get('niche') or '—'}")
            lines.append(f"- ЛПР: {c.get('decision_maker_role') or '—'}")
            lines.append(f"- Email: {c.get('contact_email') or '—'}")
            lines.append(f"- Телефон: {c.get('contact_phone') or '—'}")
            lines.append(f"- Telegram: {c.get('contact_telegram') or '—'}")
            if c.get("contacts_note"):
                lines.append(f"- Источник контактов: {c['contacts_note']}")
            if c.get("snippet"):
                lines.append(f"- Сниппет: {c['snippet']}")
            lines.append("")

    elif agent_name == "sales":
        from core.outreach_export import build_outreach_markdown

        return build_outreach_markdown(
            [m for m in (parsed.get("messages") or []) if isinstance(m, dict)],
            qualification_questions=list(parsed.get("qualification_questions") or []),
            next_steps=str(parsed.get("next_steps") or ""),
        )

    else:
        lines.append("# Результат агента")
        lines.append("```json")
        lines.append(json.dumps(parsed, ensure_ascii=False, indent=2))
        lines.append("```")

    return "\n".join(lines).strip() + "\n"


def _build_pm_payload(project: Dict[str, Any], tasks_payload: list, review_tasks: list) -> Dict[str, Any]:
    reasoning = project.get("reasoning") or ""
    if isinstance(reasoning, str) and len(reasoning) > 1200:
        reasoning = reasoning[:1200]
    return {
        "project_name": project.get("project_name") or "",
        "phase": project.get("current_phase") or "",
        "status": project.get("status") or "",
        "client": project.get("client_name") or "",
        "tokens_used": project.get("tokens_used") or 0,
        "token_budget": project.get("token_budget") or Config.TOKEN_BUDGET,
        "reasoning": reasoning,
        "goal": (project.get("goal") or "")[:800],
        "active_agents": [
            t.get("agent_name") for t in tasks_payload if t.get("status") == "in_progress"
        ],
        "tasks_total": len(tasks_payload),
        "tasks_completed": sum(1 for t in tasks_payload if t.get("status") == "completed"),
        "tasks_failed": sum(1 for t in tasks_payload if t.get("status") == "failed"),
        "review_count": len(review_tasks),
    }


def _project_view_payload(project: Dict[str, Any]) -> Dict[str, Any]:
    """Единый payload для live status и просмотра истории."""
    project_id = project.get("Id")
    tasks_payload, review_tasks = _build_tasks_payload(project_id)
    metrics = _parse_metrics(project.get("metrics"))
    iteration = metrics.get("iteration")
    if iteration is None and project.get("iteration") is not None:
        try:
            iteration = int(project.get("iteration"))
        except (TypeError, ValueError):
            iteration = 1
    try:
        iteration = max(1, int(iteration or 1))
    except (TypeError, ValueError):
        iteration = 1
    history = metrics.get("iteration_history")
    if not isinstance(history, list):
        history = []
    return {
        "project_id": project_id,
        "project_name": project.get("project_name") or "",
        "client": project.get("client_name") or "—",
        "status": project.get("status") or "",
        "phase": project.get("current_phase") or "",
        "tokens_used": project.get("tokens_used") or 0,
        "token_budget": project.get("token_budget") or Config.TOKEN_BUDGET,
        "final_report": project.get("final_report") or "",
        "metrics": metrics,
        "iteration": iteration,
        "iteration_history_count": len(history),
        "can_refine": True,
        "completed_at": project.get("completed_at") or "",
        "goal": project.get("goal") or "",
        "tasks": tasks_payload,
        "review_tasks": review_tasks,
        "pm": _build_pm_payload(project, tasks_payload, review_tasks),
    }


def _safe_filename(name: str) -> str:
    name = (name or "workflow").strip().replace(" ", "_")
    name = re.sub(r"[^\w.\-а-яА-ЯёЁ]+", "", name, flags=re.UNICODE)
    return name[:120] or "workflow"


