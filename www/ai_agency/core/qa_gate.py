"""
QA Gate — проверка результатов задач через QA-агента с Pydantic-валидацией.
Выделен из orchestrator.py для снижения связности.
"""
import json
import logging
from typing import TYPE_CHECKING, Any, Dict

from pydantic import BaseModel

from .schemas import QAResponse, call_and_parse_llm, get_model_schema
from .utils import call_llm, load_prompt, log_to_agent_logs, send_telegram_alert, update_last_agent_log

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

logger = logging.getLogger("QAGate")


_AGENT_QA_CHECKLISTS: dict = {
    "client_hunter": """
СПЕЦИФИКА ПРОВЕРКИ CLIENT HUNTER (МОНЕТИЗАЦИЯ):
- Источник каждого клиента должен быть только "google" (открытый поиск)
- Не должно быть выдуманных телефонов/email/Telegram без URL из Google
- У каждого клиента есть usp с headline, value_proposition, differentiators, call_to_action
- clients согласованы с google_search_results во входных данных (если они были)
- В clients НЕТ SaaS/CRM/статей (YClients, Bitrix, Albato, «как автоматизировать клинику»)
- Клиенты соответствуют ICP из goal/task (тип бизнеса), а не «любой медцентр из статьи»
- По возможности заполнены decision_maker_role и контакты (email/phone) из открытых данных
- search_queries — prospect-ориентированные (сайт/записаться/город), не «ниша + CRM»
- search_queries не пустой, если total_found > 0
""",
    "lead_hunter": """
СПЕЦИФИКА ПРОВЕРКИ LEAD HUNTER:
- Лиды только из google_search_results (OpenSERP); нет выдуманных компаний
- У каждого лида есть website или source_url из выдачи
- Нет фейковых @telegram / email / телефонов без подтверждения в сниппете
- Если SERP пуст — leads_found=[] и честные notes
- source не должен утверждать «парсинг WB/Telegram», если данных не было
""",
    "architect": """
СПЕЦИФИКА ПРОВЕРКИ АРХИТЕКТУРЫ:
- workflow_blueprint должен содержать nodes[] со ВСЕМИ нодами (включая Set-ноды для сохранения контекста перед HTTP)
- connections[] должны описывать ВСЕ связи; ни одна нода не должна быть изолированной
- Каждая IF-нода должна иметь явное condition (не пустое)
- Если после HTTP-вызова нужен id исходной записи (NocoDB rowId и т.п.) — ОБЯЗАТЕЛЬНА Set-нода ДО HTTP для сохранения этого id
- field_mapping[] должен описывать, какие поля откуда берутся (source → target)
- ЧЕК-ЛИСТ УСТОЙЧИВОСТИ в error_handling[] (все 5 пунктов):
  (1) тип ошибки transient vs permanent; (2) идемпотентность/что можно повторять;
  (3) backoff + jitter; (4) maxTries / нет бесконечного цикла; (5) резерв: лог + алерт + сохранение данных
- Rate limits API указаны в systems[].limitations заранее (не только «после 429»)
""",
    "developer": """
СПЕЦИФИКА ПРОВЕРКИ n8n WORKFLOW:
- Каждая нода в nodes[] должна иметь type, typeVersion, position, parameters
- connections используют поле "index" (НЕ "inputIndex")
- Ни одна нода не должна быть изолированной (не подключённой ни к чему)
- IF-ноды: conditions.conditions — непустой массив с реальными выражениями leftValue
- nocoDb update: использует fieldsUi.fieldValues, НЕ data:{}
- credentials ключи: nocoDbApiToken, telegramApi (не "NocoDB", не "Telegram")
- После HTTP Request $json содержит ответ HTTP — upstream id должен быть сохранён в Set-ноде ДО HTTP
- ЧЕК-ЛИСТ УСТОЙЧИВОСТИ (иначе issue high/critical):
  (1) retry только для временных ошибок; (2) защита от дублей на create/send;
  (3) пауза/backoff между попытками; (4) конечный maxTries, нет tight-loop;
  (5) Error-ветка: лог + уведомление + сохранение данных
""",
    "analyst": """
СПЕЦИФИКА ПРОВЕРКИ АНАЛИЗА:
- roi_calculation содержит числовые значения (не строки)
- current_pain_points не пустой
- handoff_to_architect содержит required_integrations и constraints
- proposed_automation соответствует реальной задаче проекта (не шаблонные данные)
""",
    "tech_writer": """
СПЕЦИФИКА ПРОВЕРКИ ДОКУМЕНТАЦИИ ИНТЕГРАЦИИ:
- Есть документ type=integration_guide или tech_guide (не только user_guide/КП)
- Покрыты: цель; источник и получатель; версия API; webhook/poll; пагинация/лимиты;
  контракт данных; критичные поля; обработка ошибок; адаптер; тестирование; сопровождение
- ОБЯЗАТЕЛЬНО явно описаны: критичные поля, контракт данных, обработка ошибок
  (без них — issue severity high/critical)
- faq/checklist конкретны по интеграции (поля, коды ошибок, шаги воспроизведения)
- Нет общих маркетинговых фраз вместо технических деталей
""",
}


class QAGate:
    """Проверяет результат задачи через QA-агента с Pydantic-валидацией."""

    def __init__(self, orchestrator: "Orchestrator") -> None:
        self.orch = orchestrator
        # Хранит текст последнего сформированного фидбека.
        # Используется handle_architect (update_status=False — QA не пишет в БД сам).
        self.last_feedback: str = ""

    def run(
        self,
        task: Dict[str, Any],
        task_db_id: Any,
        task_name: str,
        agent_name: str,
        agent_response: Any,
        task_description: str,
        iteration_count: int,
        max_iter: int,
        update_status: bool = True,
    ) -> bool:
        """
        Запускает QA-проверку результата задачи.

        Returns:
            True если QA пройден, False если нет.
        """
        project_id = self.orch.current_project.get("Id")
        logger.info(f"🔍 QA-проверка для задачи {task_name}...")

        # Конвертируем ответ агента в строку
        if isinstance(agent_response, BaseModel):
            agent_response_str = agent_response.model_dump_json(indent=2)
        elif isinstance(agent_response, dict):
            agent_response_str = json.dumps(agent_response, indent=2, ensure_ascii=False)
        else:
            agent_response_str = str(agent_response)

        schema = get_model_schema(QAResponse)
        schema_prompt = load_prompt("qa") + f"""

═══════════════════════════════════════════════════════════
СТРОГАЯ СТРУКТУРА ОТВЕТА (JSON Schema)
═══════════════════════════════════════════════════════════

Ты ДОЛЖЕН вернуть ТОЛЬКО валидный JSON, строго соответствующий этой схеме:

{schema}

ПРАВИЛА:
1. Верни ТОЛЬКО JSON, без комментариев до или после
2. Все обязательные поля должны быть заполнены
3. Типы данных должны точно соответствовать схеме
4. Убедись, что все скобки и кавычки закрыты

═══════════════════════════════════════════════════════════
"""

        agent_checklist = _AGENT_QA_CHECKLISTS.get(agent_name, "")
        qa_task = f"""
ЗАДАЧА: {task_description}
АГЕНТ: {agent_name}

РЕЗУЛЬТАТ ДЛЯ ПРОВЕРКИ (длина: {len(agent_response_str)} символов):
{agent_response_str[:32000]}

ПРОВЕРЬ:
1. Соответствует ли результат задаче?
2. Нет ли ошибок или противоречий?
3. Достаточно ли данных для следующих задач?
4. Валиден ли JSON?
{agent_checklist}
ВАЖНО: Если результат соответствует задаче и не содержит критических ошибок,
установи tests_failed=0 и не добавляй issues с severity=critical или high.
"""

        try:
            qa_response, qa_tokens = call_and_parse_llm(
                call_llm_func=call_llm,
                agent_name="qa",
                system_prompt=schema_prompt,
                user_task=qa_task,
                response_model=QAResponse,
                max_retries=2,
            )

            logger.info(f"✅ QA ответ для {task_name}: {qa_response.summary}")
            logger.info(
                f"   Тестов: {qa_response.tests_total}, "
                f"пройдено: {qa_response.tests_passed}, "
                f"провалено: {qa_response.tests_failed}"
            )
            for issue in qa_response.issues:
                logger.info(
                    f"   [{issue.severity.upper()}] {issue.description} "
                    f"— {issue.location}"
                )

            has_critical = any(i.severity in ("critical", "high") for i in qa_response.issues)
            qa_approved = (qa_response.tests_failed == 0) and not has_critical
            logger.info(f"   QA approved: {qa_approved}")

            # Обновляем бюджет токенов
            self.orch.current_project["tokens_used"] = (
                self.orch.current_project.get("tokens_used", 0) or 0
            ) + qa_tokens
            self.orch.projects_db.update_project(
                project_id, {"tokens_used": self.orch.current_project["tokens_used"]}
            )

            qa_feedback_text = self._build_feedback(qa_response)
            # Сохраняем последний фидбек для handle_architect (update_status=False)
            self.last_feedback = qa_feedback_text

            log_to_agent_logs(
                project_id=project_id,
                agent_name="qa",
                status="completed" if qa_approved else "needs_review",
                task_description=(
                    f"QA-проверка {task_name}: "
                    f"{'✅ ПРОШЁЛ' if qa_approved else '❌ НЕ ПРОШЁЛ'}"
                ),
                full_response=qa_response.model_dump_json(indent=2),
                tokens_used=qa_tokens,
            )

            if not update_status:
                return qa_approved

            if qa_approved:
                try:
                    from .agent_context import record_attempt
                    record_attempt(
                        project_id=project_id,
                        task_id=task_name,
                        agent_name=agent_name,
                        iteration=iteration_count + 1,
                        status="approved",
                        feedback=qa_feedback_text,
                        artifact=agent_response if not isinstance(agent_response, str) else None,
                    )
                except Exception as e:
                    logger.debug("AgentContext approved skip: %s", e)
                self.orch.tasks_db.update_task(
                    task_db_id,
                    {
                        "status": "completed",
                        "qa_approved": "true",
                        "qa_feedback": qa_feedback_text,
                    },
                )
                update_last_agent_log(project_id, agent_name, "completed")
                logger.info(f"✅ Задача {task_name} ({agent_name}) прошла QA")
                return True

            logger.warning(f"⚠️ QA не прошёл для {task_name}: {qa_feedback_text[:500]}")
            try:
                from .agent_context import record_attempt
                issues = []
                if hasattr(qa_response, "issues"):
                    issues = [
                        f"[{getattr(i, 'severity', '?')}] {getattr(i, 'description', i)}"
                        for i in (qa_response.issues or [])
                    ][:30]
                record_attempt(
                    project_id=project_id,
                    task_id=task_name,
                    agent_name=agent_name,
                    iteration=iteration_count + 1,
                    status="rejected",
                    feedback=qa_feedback_text,
                    issues=issues,
                    artifact=agent_response if not isinstance(agent_response, str) else agent_response,
                )
            except Exception as e:
                logger.debug("AgentContext reject skip: %s", e)
            self.orch.tasks_db.update_task(
                task_db_id,
                {
                    "status": "pending",
                    "qa_approved": "false",
                    "qa_feedback": qa_feedback_text,
                },
            )
            update_last_agent_log(project_id, agent_name, "needs_review")

            if iteration_count + 1 >= max_iter:
                logger.error(f"❌ Задача {task_name} провалена после {max_iter} итераций QA")
                self.orch.tasks_db.update_task(task_db_id, {"status": "failed"})
                update_last_agent_log(project_id, agent_name, "failed")
                send_telegram_alert(
                    f"🚨 <b>Агент застрял в цикле QA</b>\n\n"
                    f"Агент: <code>{agent_name}</code>\n"
                    f"Задача: <code>{task_name}</code>\n"
                    f"QA-итераций выполнено: {iteration_count + 1}/{max_iter}\n"
                    f"Последняя ошибка: {qa_feedback_text[:300]}\n\n"
                    f"Задача переведена в статус <b>failed</b>. "
                    f"Требуется ручное вмешательство или декомпозиция задачи."
                )

            return False

        except Exception as e:
            logger.error(f"❌ Ошибка QA: {e}", exc_info=True)
            self.last_feedback = f"Ошибка QA: {str(e)[:500]}"
            if update_status:
                self.orch.tasks_db.update_task(
                    task_db_id,
                    {
                        "status": "pending",
                        "qa_approved": "false",
                        "qa_feedback": f"Ошибка QA: {str(e)[:500]}",
                    },
                )
            return False

    @staticmethod
    def _build_feedback(qa_response: QAResponse) -> str:
        """Формирует текст обратной связи из QAResponse."""
        parts = [qa_response.summary]
        if qa_response.issues:
            parts.append("\nНайденные проблемы:")
            for issue in qa_response.issues:
                parts.append(
                    f"- [{issue.severity.upper()}] {issue.description} "
                    f"(в {issue.location}) → {issue.recommendation}"
                )
        if qa_response.warnings:
            parts.append("\nПредупреждения:")
            parts.extend(f"- {w}" for w in qa_response.warnings)
        return "\n".join(parts)
