"""
Обработчики специализированных агентов с нестандартной логикой.
Выделены из orchestrator.py для снижения связности.

════════════════════════════════════════════════════════════
ПОЧЕМУ НЕКОТОРЫЕ АГЕНТЫ ИМЕЮТ СПЕЦ-ХЕНДЛЕРЫ, А НЕ QA GATE?
════════════════════════════════════════════════════════════

Стандартный путь (QA Gate):
    developer → qa_gate.run() → статус completed/failed
    crm_customizer → qa_gate.run() → …
    tech_writer → qa_gate.run() → …

Они производят самодостаточные артефакты (workflow JSON, CRM-схема,
документация), которые нужно валидировать независимо от контекста проекта.
QA Gate — отдельный LLM-вызов, который проверяет корректность артефакта.

Спец-хендлеры нужны, когда агент:
1. НАКАПЛИВАЕТ КОНТЕКСТ для следующих агентов в цепочке:
   - analyst → сохраняет roi, pain_points, handoff_to_architect
     в current_project["analyst_context"] → передаётся architect
   - lead_hunter → сохраняет список лидов + handoff_to_sales
     в current_project["leads_context"] → передаётся sales
   - client_hunter → Google-only поиск + УТП в client_hunter_context
     → handoff_to_sales
   - sales → сохраняет сообщения и qualification
     в current_project["sales_context"] → передаётся analyst

2. ЗАПУСКАЕТ ДОПОЛНИТЕЛЬНЫЙ LLM-вызов после QA:
   - architect → после QA Gate просит PM декомпозировать архитектуру
     на dev_* подзадачи (2-й LLM-вызов с PMDecomposition-моделью)

3. ИМЕЕТ FALLBACK-ЛОГИКУ РЕАЛЬНОГО ИНСТРУМЕНТА:
   - lead_hunter → OpenSERP inject + фильтр галлюцинаций; leads_context
   - client_hunter → OpenSERP inject + УТП в client_hunter_context

Агенты со спец-хендлерами авто-подтверждают QA ("qa_approved": "true")
и сами проставляют статус "completed", не используя QA Gate.
"""
import json
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, Optional

from pydantic import BaseModel

from .config import Config
from .dev_decomposition import (
    MODE_FULL,
    build_developer_input_data,
    extract_workflow_units,
    normalize_developer_subtasks,
)
from .task_ids import is_developer_placeholder_task
from .schemas import (
    AnalystResponse,
    ClientHunterResponse,
    LeadHunterResponse,
    PMDecomposition,
    SalesResponse,
    call_and_parse_llm,
)
from .utils import call_llm, log_to_agent_logs

if TYPE_CHECKING:
    from .orchestrator import Orchestrator
    from .qa_gate import QAGate

logger = logging.getLogger("AgentHandlers")


class AgentHandlers:
    """Спецобработчики для агентов с нестандартной логикой."""

    def __init__(self, orchestrator: "Orchestrator", qa_gate: "QAGate") -> None:
        self.orch = orchestrator
        self.qa_gate = qa_gate

    # ──────────────────────────────────────────────────────────────────────────
    # ARCHITECT: QA → декомпозиция на подзадачи dev_*
    # ──────────────────────────────────────────────────────────────────────────

    def handle_architect(
        self, task: Dict, task_db_id: Any, task_name: str, agent_response: Any, pm_prompt: str
    ) -> bool:
        """Architect: QA-проверка, затем PM декомпозирует на подзадачи developer."""
        project_id = self.orch.current_project.get("Id")

        # QA без обновления статуса задачи (контроль остаётся здесь).
        # update_status=False: QA Gate не пишет в БД сам, мы делаем это ниже с реальным фидбеком.
        # iteration_count передаём реальный, чтобы QA Gate мог в будущем использовать его.
        iteration_count = task.get("iteration_count", 0) or 0
        max_iter = task.get("max_iterations", 3) or 3
        logger.info(f"🔍 QA-проверка для architect-задачи {task_name} (итерация {iteration_count}/{max_iter})...")
        qa_ok = self.qa_gate.run(
            task, task_db_id, task_name, "architect",
            agent_response, task.get("task_description", ""),
            iteration_count=iteration_count, max_iter=max_iter, update_status=False,
        )

        if not qa_ok:
            # Берём реальный фидбек, сохранённый в qa_gate.last_feedback.
            # Ранее здесь было "QA не прошёл, требуется доработка" — architect не знал, что исправлять.
            real_feedback = self.qa_gate.last_feedback or "QA не прошёл, требуется доработка"
            if iteration_count + 1 >= max_iter:
                logger.error(f"❌ Задача {task_name} провалена после {max_iter} QA-итераций")
                self.orch.tasks_db.update_task(task_db_id, {
                    "status": "failed",
                    "qa_approved": "false",
                    "qa_feedback": real_feedback,
                })
            else:
                logger.warning(f"⚠️ QA не прошёл для {task_name}, возврат в pending. Фидбек: {real_feedback[:200]}")
                self.orch.tasks_db.update_task(task_db_id, {
                    "status": "pending",
                    "iteration_count": iteration_count + 1,
                    "qa_approved": "false",
                    "qa_feedback": real_feedback,
                })
            return False

        logger.info(f"✅ Architect прошёл QA. Декомпозиция на подзадачи...")
        agent_response_str = _to_str(agent_response)

        # Node-level blueprint от архитектора — source of truth для developer.
        # Полный blueprint передаётся ТОЛЬКО задаче artifact_mode=full_workflow
        # (см. core/dev_decomposition.py). Иначе каждая dev_* сериализует весь
        # сценарий заново → дубли workflow.
        # Direction AE: несколько независимых workflow → несколько full_workflow задач.
        blueprint = None
        if isinstance(agent_response, BaseModel):
            blueprint = getattr(agent_response, "handoff_to_developer", None)
        elif isinstance(agent_response, dict):
            blueprint = agent_response.get("handoff_to_developer")
        workflow_units = extract_workflow_units(blueprint)
        blueprint_str = (
            json.dumps(blueprint, ensure_ascii=False, indent=2) if blueprint else ""
        )
        has_blueprint = bool(
            workflow_units
            or (blueprint_str and blueprint_str.strip() not in ("{}", "null"))
        )

        units_hint = ""
        if len(workflow_units) > 1:
            units_hint = (
                "\nВ АРХИТЕКТУРЕ НЕСКОЛЬКО НЕЗАВИСИМЫХ WORKFLOW "
                f"({len(workflow_units)}):\n"
                + "\n".join(
                    f"- {u['workflow_id']}: {u['name']}" for u in workflow_units
                )
                + "\nСоздай РОВНО по одной full_workflow-задаче на каждый "
                "(поле workflow_id в subtask). НЕ объединяй их в одну задачу.\n"
            )

        decompose_prompt = f"""
Ты — Project Manager. Архитектор завершил проектирование. Разбей работу для developer.

АРХИТЕКТУРА ОТ ARCHITECT:
{agent_response_str[:16000]}

ЦЕЛЬ ПРОЕКТА:
{self.orch.current_project.get('goal', '')}
{units_hint}
ФОРМАТ ОТВЕТА (строго JSON):
{{
    "subtasks": [
        {{
            "subtask_id": "dev_001",
            "description": "Собрать единый n8n workflow по blueprint",
            "depends_on": [],
            "context": "Краткий контекст",
            "artifact_mode": "full_workflow",
            "workflow_id": "wf_1",
            "assigned_node_names": []
        }}
    ],
    "pm_comment": "Один workflow — одна задача сборки."
}}

ПРАВИЛА (КРИТИЧНО):
1. Если в архитектуре ОДИН n8n-сценарий (один workflow_blueprint) — создай
   РОВНО ОДНУ задачу с artifact_mode="full_workflow". НЕ режь retry / ошибки /
   журнал / идемпотентность на отдельные developer-задачи с n8n JSON.
2. Если в цели или у architect НЕСКОЛЬКО независимых workflow
   (разные триггеры/продукты, workflow_blueprints[]) — создай ОТДЕЛЬНУЮ
   full_workflow-задачу на КАЖДЫЙ (с workflow_id). Запрещено писать
   «сделай два workflow» в одной задаче.
3. artifact_mode:
   - "full_workflow" — возвращает n8n_json (1 workflow на задачу);
   - "prep" — таблицы CRM, credentials, env (без n8n_json);
   - "spec" — текстовая спецификация куска (без n8n_json). Редко нужно.
4. Шаги цели вроде «базовый сценарий / 4 ошибки / retry / журнал / fallback» —
   это требования ВНУТРИ одной full_workflow-задачи, а не отдельные subtasks.
5. Максимум 5–7 подзадач; prep могут идти до full_workflow (depends_on).
6. Верни ТОЛЬКО валидный JSON.
"""

        try:
            pm_decision, pm_tokens = call_and_parse_llm(
                call_llm_func=call_llm,
                agent_name="PM",
                system_prompt=pm_prompt,
                user_task=decompose_prompt,
                response_model=PMDecomposition,
                max_retries=3,
            )

            self.orch.add_tokens(pm_tokens)

            raw_subtasks = pm_decision.subtasks if hasattr(pm_decision, "subtasks") else []
            if not raw_subtasks:
                logger.error("❌ PM не вернул подзадачи")
                self.orch.tasks_db.update_task(task_db_id, {"status": "failed"})
                return False

            subtasks = normalize_developer_subtasks(
                raw_subtasks,
                has_blueprint=has_blueprint,
                workflow_units=workflow_units,
            )
            logger.info(
                "📦 PM декомпозировал на %s подзадач → после нормализации %s "
                "(blueprint=%s, units=%s, full=%s)",
                len(raw_subtasks),
                len(subtasks),
                has_blueprint,
                len(workflow_units),
                sum(1 for s in subtasks if s.get("artifact_mode") == MODE_FULL),
            )

            units_by_id = {u["workflow_id"]: u for u in workflow_units}
            sibling_names = [u["name"] for u in workflow_units]

            subtask_ids = []
            for sd in subtasks:
                subtask_ids.append(sd.get("subtask_id", ""))
                wid = sd.get("workflow_id") or ""
                unit = units_by_id.get(wid)
                if not unit and len(workflow_units) == 1 and sd.get("artifact_mode") == MODE_FULL:
                    unit = workflow_units[0]
                siblings = [
                    n for n in sibling_names
                    if n and n != (unit or {}).get("name") and n != sd.get("workflow_name")
                ] if sd.get("artifact_mode") == MODE_FULL else []
                input_payload = build_developer_input_data(
                    subtask=sd,
                    architecture_summary=agent_response_str,
                    blueprint=blueprint,
                    blueprint_str=blueprint_str,
                    workflow_unit=unit,
                    sibling_workflow_names=siblings or None,
                )
                self.orch.tasks_db.create_task({
                    "task_id": sd.get("subtask_id"),
                    "project_id": project_id,
                    "agent_name": "developer",
                    "task_description": sd.get("description"),
                    "input_data": json.dumps(input_payload, ensure_ascii=False),
                    "status": "pending",
                    "depends_on": json.dumps(sd.get("depends_on", []), ensure_ascii=False),
                    "iteration_count": 0,
                    "max_iterations": Config.DEVELOPER_MAX_ITERATIONS,
                    "qa_approved": "pending",
                    "created_at": datetime.now().isoformat(),
                })
                logger.info(
                    "  → Подзадача %s [%s] workflow_id=%s",
                    sd.get("subtask_id"),
                    sd.get("artifact_mode"),
                    sd.get("workflow_id") or "-",
                )

            # Placeholder developer (task_003 / iterN_task_*) → failed, чтобы не
            # исполнялся вместо сабтасков. check_and_complete_parent_tasks вернёт
            # completed, когда все (iterN_)dev_* завершатся.
            try:
                all_project_tasks = self.orch.tasks_db.get_tasks_by_project(project_id)
                for pt in all_project_tasks:
                    if (
                        is_developer_placeholder_task(pt)
                        and pt.get("status") == "pending"
                    ):
                        pt_task_id = pt.get("task_id", "")
                        self.orch.tasks_db.update_task(pt.get("Id"), {
                            "status": "failed",
                            "qa_feedback": (
                                f"Placeholder: заменена подзадачами {', '.join(subtask_ids)}. "
                                f"Будет помечена completed автоматически после выполнения всех dev_*."
                            ),
                        })
                        logger.info(
                            f"⏭️ Placeholder developer-задача '{pt_task_id}' переведена в failed "
                            f"(заменена {len(subtasks)} подзадачами)"
                        )
            except Exception as e:
                logger.warning(f"⚠️ Не удалось пометить placeholder developer-задачу: {e}")

            self.orch.tasks_db.update_task(task_db_id, {
                "status": "completed",
                "qa_approved": "true",
                "qa_feedback": (
                    f"Декомпозирована на {len(subtasks)} подзадач: {', '.join(subtask_ids)}"
                ),
            })

            full_response = (
                pm_decision.model_dump_json(indent=2)
                if hasattr(pm_decision, "model_dump_json")
                else json.dumps(pm_decision, ensure_ascii=False)
            )
            log_to_agent_logs(
                project_id=project_id,
                agent_name="PM",
                status="completed",
                task_description=f"Декомпозиция {task_name} на {len(subtasks)} подзадач",
                full_response=full_response,
                tokens_used=pm_tokens,
            )
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка декомпозиции: {e}", exc_info=True)
            self.orch.tasks_db.update_task(task_db_id, {"status": "failed"})
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # CLIENT HUNTER: Google-only + УТП → контекст монетизации
    # ──────────────────────────────────────────────────────────────────────────

    def handle_client_hunter(
        self, task: Dict, task_db_id: Any, task_name: str, agent_response: Any, pm_prompt: str
    ) -> bool:
        """Сохраняет клиентов и УТП; источник — только Google (открытый поиск)."""
        try:
            if isinstance(agent_response, ClientHunterResponse):
                response = agent_response
            elif isinstance(agent_response, str) and agent_response.strip().startswith("{"):
                response = ClientHunterResponse(**json.loads(agent_response))
            else:
                response = ClientHunterResponse(
                    summary="Нет структурированного ответа",
                    search_queries=[],
                    clients=[],
                    total_found=0,
                    notes="LLM не вернул ClientHunterResponse",
                )

            # Жёстко: source только google
            clients_payload = []
            for c in response.clients:
                usp = c.usp
                clients_payload.append({
                    "company_name": c.company_name,
                    "website": c.website,
                    "snippet": c.snippet,
                    "niche": c.niche,
                    "decision_maker_role": c.decision_maker_role,
                    "contact_email": c.contact_email,
                    "contact_phone": c.contact_phone,
                    "contact_telegram": c.contact_telegram,
                    "contacts_note": c.contacts_note,
                    "pain_hypothesis": list(c.pain_hypothesis or []),
                    "usp": {
                        "headline": usp.headline,
                        "value_proposition": usp.value_proposition,
                        "differentiators": list(usp.differentiators or []),
                        "call_to_action": usp.call_to_action,
                    },
                    "source": "google",
                    "source_query": c.source_query,
                })

            # Открытые контакты с website лида (не маркетплейсы) — для ЛПР/outreach
            if Config.CLIENT_HUNTER_SCRAPE_CONTACTS and clients_payload:
                from .outreach_export import enrich_clients_with_website_contacts

                clients_payload = enrich_clients_with_website_contacts(
                    clients_payload,
                    max_scrapes=Config.CLIENT_HUNTER_SCRAPE_MAX,
                )

            self.orch.current_project["client_hunter_context"] = clients_payload
            if response.handoff_to_sales:
                self.orch.current_project["client_hunter_handoff_to_sales"] = (
                    response.handoff_to_sales
                )
                logger.info(
                    "client_hunter handoff_to_sales: %s",
                    list(response.handoff_to_sales.keys()),
                )

            # Также кладём в leads_context упрощённый вид — sales может использовать оба
            if "leads_context" not in self.orch.current_project:
                self.orch.current_project["leads_context"] = []
            for c in clients_payload:
                self.orch.current_project["leads_context"].append({
                    "company_name": c["company_name"],
                    "marketplace": "open_web",
                    "category": c.get("niche") or "",
                    "pain_points": c.get("pain_hypothesis") or [],
                    "contact_telegram": c.get("contact_telegram"),
                    "contact_email": c.get("contact_email"),
                    "contact_phone": c.get("contact_phone"),
                    "decision_maker_role": c.get("decision_maker_role"),
                    "source": "google",
                    "website": c.get("website"),
                    "usp": c.get("usp"),
                })

            contacts_n = sum(
                1
                for c in clients_payload
                if c.get("contact_email") or c.get("contact_phone")
            )
            # Перезаписываем output_data обогащёнными контактами (для дашборда/выгрузки)
            enriched_output = response.model_dump()
            enriched_output["clients"] = clients_payload
            enriched_output["total_found"] = len(clients_payload)

            if task_db_id:
                self.orch.tasks_db.update_task(task_db_id, {
                    "status": "completed",
                    "qa_approved": "true",
                    "output_data": json.dumps(enriched_output, ensure_ascii=False),
                    "qa_feedback": (
                        f"Google: {len(clients_payload)} клиентов с УТП; "
                        f"контактов email/тел: {contacts_n} "
                        f"(queries={len(response.search_queries)})"
                    ),
                })
            logger.info(
                "✅ Client Hunter: %s клиентов с УТП, контактов=%s",
                len(clients_payload), contacts_n,
            )
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка Client Hunter: {e}", exc_info=True)
            if task_db_id:
                self.orch.tasks_db.update_task(task_db_id, {
                    "status": "failed",
                    "qa_feedback": f"Ошибка client_hunter: {str(e)[:400]}",
                })
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # LEAD HUNTER: реальный поиск + сохранение в контекст
    # ──────────────────────────────────────────────────────────────────────────

    def handle_lead_hunter(
        self, task: Dict, task_db_id: Any, task_name: str, agent_response: Any, pm_prompt: str
    ) -> bool:
        """Lead Hunter: только лиды, подтверждённые OpenSERP (анти-галлюцинации)."""
        try:
            from .lead_hunter_tools import (
                enrich_lead_contacts,
                filter_hallucinated_leads,
            )

            if isinstance(agent_response, LeadHunterResponse):
                lead_response = agent_response
            elif isinstance(agent_response, str) and agent_response.strip().startswith("{"):
                lead_response = LeadHunterResponse(**json.loads(agent_response))
            else:
                lead_response = LeadHunterResponse(
                    leads_found=[],
                    total_found=0,
                    notes="LLM не вернул LeadHunterResponse",
                )

            # SERP из input задачи (inject до LLM) — источник истины
            serp: list = []
            try:
                raw_in = task.get("input_data") or "{}"
                inp = json.loads(raw_in) if isinstance(raw_in, str) else (raw_in or {})
                serp = list(inp.get("google_search_results") or [])
            except Exception:
                serp = []
            if not serp and self.orch.current_project:
                serp = list(
                    self.orch.current_project.get("_lead_hunter_serp") or []
                )

            raw_leads = [lead.model_dump() for lead in lead_response.leads_found]
            verified = filter_hallucinated_leads(raw_leads, serp)
            if verified:
                verified = enrich_lead_contacts(verified)

            dropped = len(raw_leads) - len(verified)
            if dropped:
                logger.warning(
                    "lead_hunter: отброшено %s галлюцинированных лидов из %s",
                    dropped, len(raw_leads),
                )

            self.orch.current_project["leads_context"] = [
                {
                    "company_name": L.get("company_name"),
                    "marketplace": L.get("marketplace"),
                    "category": L.get("category"),
                    "pain_points": L.get("pain_points") or [],
                    "contact_telegram": L.get("contact_telegram"),
                    "contact_email": L.get("contact_email"),
                    "contact_phone": L.get("contact_phone"),
                    "website": L.get("website") or L.get("source_url"),
                    "source_url": L.get("source_url"),
                    "source_query": L.get("source_query"),
                    "source": L.get("source") or "openserp",
                }
                for L in verified
            ]

            if lead_response.handoff_to_sales:
                self.orch.current_project["leads_handoff_to_sales"] = (
                    lead_response.handoff_to_sales
                )

            enriched_output = lead_response.model_dump()
            enriched_output["leads_found"] = verified
            enriched_output["total_found"] = len(verified)
            if dropped:
                note = enriched_output.get("notes") or ""
                enriched_output["notes"] = (
                    f"{note} | Отфильтровано выдуманных лидов: {dropped}".strip(" |")
                )

            if task_db_id:
                self.orch.tasks_db.update_task(task_db_id, {
                    "status": "completed",
                    "qa_approved": "true",
                    "output_data": json.dumps(enriched_output, ensure_ascii=False),
                    "qa_feedback": (
                        f"OpenSERP: {len(verified)} подтверждённых лидов"
                        + (f" (отброшено выдуманных: {dropped})" if dropped else "")
                    ),
                })
            logger.info(
                "✅ Lead Hunter: %s лидов (SERP=%s, dropped=%s)",
                len(verified), len(serp), dropped,
            )
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка обработки Lead Hunter: {e}", exc_info=True)
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # SALES: сохранение сообщений и квалификации в контекст
    # ──────────────────────────────────────────────────────────────────────────

    def handle_sales(
        self, task: Dict, task_db_id: Any, task_name: str, agent_response: Any, pm_prompt: str
    ) -> bool:
        """Sales: извлекает сообщения и квалификацию, сохраняет в контекст."""
        try:
            if isinstance(agent_response, SalesResponse):
                sales_data = agent_response
            else:
                sales_data = SalesResponse(**json.loads(_to_str(agent_response)))

            logger.info(f"📝 Sales: {len(sales_data.messages)} сообщений (выгрузка, без отправки)")

            from .outreach_export import merge_messages_with_contacts

            clients = list(self.orch.current_project.get("client_hunter_context") or [])
            # Лиды lead_hunter — только с website (подтверждённые OpenSERP)
            if not clients:
                for lead in self.orch.current_project.get("leads_context") or []:
                    if not (lead.get("website") or lead.get("source_url")):
                        continue
                    clients.append({
                        "company_name": lead.get("company_name"),
                        "website": lead.get("website") or lead.get("source_url"),
                        "contact_email": lead.get("contact_email"),
                        "contact_phone": lead.get("contact_phone"),
                        "contact_telegram": lead.get("contact_telegram"),
                        "decision_maker_role": None,
                    })

            # УТП / письмо клиенту проекта (без hunter): не затирать messages —
            # иначе полный ответ остаётся только в agent_logs, а tasks → 0 писем.
            if not clients:
                fallback_name = _sales_fallback_client_name(self.orch.current_project)
                if fallback_name:
                    clients = [{
                        "company_name": fallback_name,
                        "website": None,
                        "contact_email": None,
                        "contact_phone": None,
                        "contact_telegram": None,
                        "decision_maker_role": None,
                    }]
                    logger.info(
                        "sales: нет hunter-лидов — письма для клиента проекта «%s»",
                        fallback_name,
                    )

            allowed_names = {
                _norm_person_or_company(c.get("company_name"))
                for c in clients
                if c.get("company_name")
            }
            allowed_names.discard("")

            raw_messages = [m.model_dump() for m in sales_data.messages]
            if allowed_names:
                filtered = [
                    m for m in raw_messages
                    if _lead_name_allowed(m.get("lead_name"), allowed_names)
                ]
                # Если LLM назвал получателя иначе, но клиент проекта один —
                # сохраняем письма и нормализуем lead_name (не теряем текст).
                if not filtered and raw_messages and len(allowed_names) == 1:
                    only = next(iter(allowed_names))
                    display = next(
                        (c.get("company_name") for c in clients if c.get("company_name")),
                        only,
                    )
                    for m in raw_messages:
                        m["lead_name"] = display
                    filtered = raw_messages
                    logger.info(
                        "sales: lead_name не совпал с «%s» — письма сохранены с нормализацией",
                        display,
                    )
                raw_messages = filtered
            elif raw_messages:
                # Нет ни hunter, ни client_name проекта — анти-галлюцинация
                logger.warning(
                    "sales: нет подтверждённых клиентов/лидов и client_name — письма очищены"
                )
                raw_messages = []
            messages = merge_messages_with_contacts(raw_messages, clients)

            if "sales_context" not in self.orch.current_project:
                self.orch.current_project["sales_context"] = []
            self.orch.current_project["sales_context"].append({
                "messages": messages,
                "qualification_questions": sales_data.qualification_questions,
                "next_steps": sales_data.next_steps,
                "send_mode": "manual_export_only",
            })

            enriched_output = {
                "messages": messages,
                "qualification_questions": sales_data.qualification_questions,
                "next_steps": sales_data.next_steps,
                "send_mode": "manual_export_only",
            }

            if task_db_id:
                self.orch.tasks_db.update_task(task_db_id, {
                    "status": "completed",
                    "qa_approved": "true",
                    "output_data": json.dumps(enriched_output, ensure_ascii=False),
                    "qa_feedback": (
                        f"Подготовлено {len(messages)} писем для ручной отправки "
                        f"(выгрузка .md/.json/.csv)"
                    ),
                })
            logger.info("✅ Sales: письма сохранены для выгрузки (%s)", len(messages))
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка обработки Sales: {e}", exc_info=True)
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # ANALYST: ROI + боли → контекст для architect
    # ──────────────────────────────────────────────────────────────────────────

    def handle_analyst(
        self, task: Dict, task_db_id: Any, task_name: str, agent_response: Any, pm_prompt: str
    ) -> bool:
        """Analyst: ROI, болевые точки — сохраняет контекст для architect."""
        try:
            if isinstance(agent_response, AnalystResponse):
                analyst_data = agent_response
            else:
                analyst_data = AnalystResponse(**json.loads(_to_str(agent_response)))

            roi = analyst_data.roi_calculation
            logger.info(
                f"📊 Analyst ROI: экономия {roi.cost_saved_per_month_rub} руб/мес, "
                f"окупаемость {roi.payback_period_months} мес"
            )

            analyst_context: dict = {
                "client_name": analyst_data.client_name,
                "pain_points": [pp.model_dump() for pp in analyst_data.current_pain_points],
                "proposed_automation": [pa.model_dump() for pa in analyst_data.proposed_automation],
                "roi_calculation": roi.model_dump(),
                "proposal_structure": analyst_data.proposal_structure,
            }

            # Сохраняем handoff_to_architect — ключевые ограничения/требования для architect
            # (required_integrations, data_volume_estimate, priority_automations, constraints)
            if analyst_data.handoff_to_architect:
                analyst_context["handoff_to_architect"] = analyst_data.handoff_to_architect
                logger.info(
                    f"📋 handoff_to_architect сохранён: "
                    f"{list(analyst_data.handoff_to_architect.keys())}"
                )

            self.orch.current_project["analyst_context"] = analyst_context

            if task_db_id:
                self.orch.tasks_db.update_task(task_db_id, {
                    "status": "completed",
                    "qa_approved": "true",
                    "qa_feedback": f"ROI рассчитан: экономия {roi.cost_saved_per_month_rub} руб/мес",
                })
            logger.info("✅ Analyst: анализ сохранён в контекст")
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка обработки Analyst: {e}", exc_info=True)
            return False


# ──────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции
# ──────────────────────────────────────────────────────────────────────────────

def _to_str(value: Any) -> str:
    """Конвертирует Pydantic-модель, dict или что угодно в строку."""
    if isinstance(value, BaseModel):
        return value.model_dump_json(indent=2)
    if isinstance(value, dict):
        return json.dumps(value, indent=2, ensure_ascii=False)
    return str(value)


def _norm_person_or_company(name: Any) -> str:
    return " ".join(str(name or "").strip().lower().split())


def _sales_fallback_client_name(project: Optional[Dict]) -> str:
    """Имя клиента проекта / analyst для sales без hunter-лидов."""
    if not isinstance(project, dict):
        return ""
    name = (project.get("client_name") or "").strip()
    if name:
        return name
    ctx = project.get("analyst_context")
    if isinstance(ctx, dict):
        name = (ctx.get("client_name") or "").strip()
        if name:
            return name
    if isinstance(ctx, list):
        for item in ctx:
            if isinstance(item, dict) and item.get("client_name"):
                return str(item["client_name"]).strip()
    return ""


def _lead_name_allowed(lead_name: Any, allowed_names: set) -> bool:
    """Точное или частичное совпадение имени получателя с карточкой клиента."""
    n = _norm_person_or_company(lead_name)
    if not n:
        return False
    if n in allowed_names:
        return True
    for a in allowed_names:
        if len(a) < 4 or len(n) < 4:
            continue
        if a in n or n in a:
            return True
    return False
