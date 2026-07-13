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
   - lead_hunter → если LLM не вернул лидов, вызывает Telegram search API
   - client_hunter → перед LLM подмешивает Google Custom Search;
     сохраняет клиентов + УТП в client_hunter_context

Агенты со спец-хендлерами авто-подтверждают QA ("qa_approved": "true")
и сами проставляют статус "completed", не используя QA Gate.
"""
import json
import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict

from pydantic import BaseModel

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
        # Передаётся в КАЖДУЮ dev-подзадачу целиком (не обрезается), чтобы developer
        # видел полную топологию нод, connections и field_mapping.
        blueprint = None
        if isinstance(agent_response, BaseModel):
            blueprint = getattr(agent_response, "handoff_to_developer", None)
        elif isinstance(agent_response, dict):
            blueprint = agent_response.get("handoff_to_developer")
        blueprint_str = (
            json.dumps(blueprint, ensure_ascii=False, indent=2) if blueprint else ""
        )

        decompose_prompt = f"""
Ты — Project Manager. Архитектор завершил проектирование. Разбей архитектуру на подзадачи для developer.

АРХИТЕКТУРА ОТ ARCHITECT:
{agent_response_str[:16000]}

ЦЕЛЬ ПРОЕКТА:
{self.orch.current_project.get('goal', '')}

ФОРМАТ ОТВЕТА (строго JSON):
{{
    "subtasks": [
        {{
            "subtask_id": "dev_001",
            "description": "Создать webhook для Telegram в n8n",
            "depends_on": [],
            "context": "Из архитектуры: Telegram Bot API, webhook endpoint /telegram"
        }}
    ],
    "pm_comment": "Разбил архитектуру на N подзадач."
}}

ПРАВИЛА:
- Каждая подзадача атомарна (один компонент/интеграция)
- Максимум 5-7 подзадач
- Указывай зависимости между подзадачами
- Передавай developer только релевантный контекст
- Если в архитектуре есть handoff_to_developer.workflow_blueprint —
  в поле context каждой подзадачи укажи, КАКИЕ ноды blueprint она реализует
  (по name), и какие connections/field_mapping к ним относятся.
- Верни ТОЛЬКО валидный JSON.
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

            self.orch.current_project["tokens_used"] = (
                self.orch.current_project.get("tokens_used", 0) or 0
            ) + pm_tokens
            self.orch.projects_db.update_project(
                project_id, {"tokens_used": self.orch.current_project["tokens_used"]}
            )

            subtasks = pm_decision.subtasks if hasattr(pm_decision, "subtasks") else []
            if not subtasks:
                logger.error("❌ PM не вернул подзадачи")
                self.orch.tasks_db.update_task(task_db_id, {"status": "failed"})
                return False

            logger.info(f"📦 PM декомпозировал на {len(subtasks)} подзадач")

            subtask_ids = []
            for subtask in subtasks:
                sd = subtask.model_dump() if hasattr(subtask, "model_dump") else subtask
                subtask_ids.append(sd.get("subtask_id", ""))
                self.orch.tasks_db.create_task({
                    "task_id": sd.get("subtask_id"),
                    "project_id": project_id,
                    "agent_name": "developer",
                    "task_description": sd.get("description"),
                    "input_data": json.dumps({
                        "context": sd.get("context", ""),
                        "architecture_summary": agent_response_str[:2000],
                        "workflow_blueprint": blueprint_str,
                    }, ensure_ascii=False),
                    "status": "pending",
                    "depends_on": json.dumps(sd.get("depends_on", []), ensure_ascii=False),
                    "iteration_count": 0,
                    "max_iterations": 3,
                    "qa_approved": "pending",
                    "created_at": datetime.now().isoformat(),
                })
                logger.info(f"  → Подзадача создана: {sd.get('subtask_id')}")

            # Помечаем placeholder developer-задачу из initial task graph как "пропущена".
            # Проблема: после завершения architect в pending остаётся задача с agent_name=developer
            # (например task_003) с depends_on=[task_architect]. Оркестратор выбирал её ПЕРВОЙ,
            # developer выполнял весь workflow сразу, а dev_001/dev_002 запускались ПОСЛЕ qa/tech_writer.
            # Решение: находим placeholder-задачу и сразу помечаем её failed (не pending → не выполняется).
            # check_and_complete_parent_tasks позже переведёт её в completed, когда все dev_* готовы.
            # Каждая dev_* проходит QA Gate в TaskExecutor (отдельные qa_dev_* не создаём).
            try:
                all_project_tasks = self.orch.tasks_db.get_tasks_by_project(project_id)
                for pt in all_project_tasks:
                    pt_task_id = pt.get("task_id", "")
                    if (
                        pt.get("agent_name") == "developer"
                        and pt.get("status") == "pending"
                        and not pt_task_id.startswith("dev_")
                    ):
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
                    "contact_telegram": None,
                    "contact_email": None,
                    "contact_phone": None,
                    "source": "google",
                    "website": c.get("website"),
                    "usp": c.get("usp"),
                })

            if task_db_id:
                self.orch.tasks_db.update_task(task_db_id, {
                    "status": "completed",
                    "qa_approved": "true",
                    "qa_feedback": (
                        f"Google: {response.total_found} клиентов с УТП "
                        f"(queries={len(response.search_queries)})"
                    ),
                })
            logger.info(
                "✅ Client Hunter: %s клиентов с УТП сохранено",
                response.total_found,
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
        """Lead Hunter: парсит ответ агента или выполняет реальный поиск."""
        try:
            # Пробуем использовать уже готовый ответ агента (Pydantic или JSON-строка)
            if isinstance(agent_response, LeadHunterResponse):
                lead_response = agent_response
            elif isinstance(agent_response, str) and agent_response.strip().startswith("{"):
                lead_response = LeadHunterResponse(**json.loads(agent_response))
            else:
                # Fallback: реальный поиск через Telegram
                lead_response = self._search_leads_real(task)

            if "leads_context" not in self.orch.current_project:
                self.orch.current_project["leads_context"] = []
            for lead in lead_response.leads_found:
                self.orch.current_project["leads_context"].append({
                    "company_name": lead.company_name,
                    "marketplace": lead.marketplace,
                    "category": lead.category,
                    "pain_points": lead.pain_points,
                    "contact_telegram": lead.contact_telegram,
                    "contact_email": lead.contact_email,
                    "contact_phone": lead.contact_phone,
                    "source": lead.source,
                })

            # Сохраняем handoff_to_sales — рекомендации Sales-агенту (тон, ключевые боли)
            if lead_response.handoff_to_sales:
                self.orch.current_project["leads_handoff_to_sales"] = lead_response.handoff_to_sales
                logger.info(
                    f"📋 handoff_to_sales сохранён: {list(lead_response.handoff_to_sales.keys())}"
                )

            if task_db_id:
                self.orch.tasks_db.update_task(task_db_id, {
                    "status": "completed",
                    "qa_approved": "true",
                    "qa_feedback": f"Найдено {lead_response.total_found} лидов",
                })
            logger.info(f"✅ Lead Hunter: {lead_response.total_found} лидов сохранено в контекст")
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка обработки Lead Hunter: {e}", exc_info=True)
            return False

    def _search_leads_real(self, task: Dict) -> LeadHunterResponse:
        """Реальный поиск лидов через Telegram."""
        from .lead_tools import lead_tools

        task_description = task.get("task_description", "")

        # Определяем категорию из описания задачи
        category = "Одежда"
        desc_lower = task_description.lower()
        if "электроник" in desc_lower:
            category = "Электроника"
        elif "товар" in desc_lower and "дом" in desc_lower:
            category = "Товары для дома"
        elif "косметик" in desc_lower:
            category = "Косметика"

        reviews_match = re.search(r"(\d+)\s*\+?\s*отзыв", desc_lower)
        min_reviews = int(reviews_match.group(1)) if reviews_match else 1000
        logger.info(f"🔍 Поиск лидов: категория={category}, мин. отзывов={min_reviews}")

        raw_leads = []
        for ch in lead_tools.search_telegram_channels(f"селлеры WB {category}"):
            raw_leads.append({
                "company_name": ch["name"],
                "marketplace": "Wildberries/Ozon",
                "category": category,
                "estimated_revenue": None,
                "pain_points": ["Активное сообщество селлеров"],
                "contact_telegram": ch.get("link"),
                "contact_email": None,
                "contact_phone": None,
                "source": "Telegram",
            })

        return LeadHunterResponse(
            leads_found=raw_leads[:20],
            total_found=len(raw_leads),
            notes=f"Поиск Telegram. Найдено {len(raw_leads)} лидов.",
        )

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

            logger.info(f"📝 Sales: {len(sales_data.messages)} сообщений")

            if "sales_context" not in self.orch.current_project:
                self.orch.current_project["sales_context"] = []
            self.orch.current_project["sales_context"].append({
                "messages": [m.model_dump() for m in sales_data.messages],
                "qualification_questions": sales_data.qualification_questions,
                "next_steps": sales_data.next_steps,
            })

            if task_db_id:
                self.orch.tasks_db.update_task(task_db_id, {
                    "status": "completed",
                    "qa_approved": "true",
                    "qa_feedback": f"Отправлено {len(sales_data.messages)} сообщений",
                })
            logger.info("✅ Sales: результаты сохранены в контекст")
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
