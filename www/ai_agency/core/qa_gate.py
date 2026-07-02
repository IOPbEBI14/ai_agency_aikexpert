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


class QAGate:
    """Проверяет результат задачи через QA-агента с Pydantic-валидацией."""

    def __init__(self, orchestrator: "Orchestrator") -> None:
        self.orch = orchestrator

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
