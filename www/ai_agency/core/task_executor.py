"""
TaskExecutor — выполнение одной задачи с Pydantic-валидацией.
Выделен из orchestrator.py для снижения связности.
"""
import json
import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from .config import Config
from .n8n_validator import build_n8n_feedback, validate_n8n_workflow
from .schemas import AGENT_MODELS, call_and_parse_llm, get_model_schema
from .utils import (
    build_agent_task,
    call_llm,
    load_prompt,
    log_to_agent_logs,
    send_telegram_alert,
    update_last_agent_log,
    validate_output_json,
)

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

logger = logging.getLogger("TaskExecutor")


class TaskExecutor:
    """
    Выполняет одну задачу: загружает промпт, вызывает агента, диспатчит QA.

    Для специальных агентов (architect, lead_hunter, sales, analyst) делегирует
    обработку через прокси-методы оркестратора (чтобы тесты могли патчить их).
    """

    def __init__(self, orchestrator: "Orchestrator") -> None:
        self.orch = orchestrator
        self.agent_prompts_cache: Dict[str, str] = {}

    def execute(
        self,
        task: Dict[str, Any],
        pm_prompt: str,
        all_tasks: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """
        Выполняет задачу: промпт → LLM → Pydantic-валидация → QA / спец-обработчик.

        Args:
            task:      Запись задачи из NocoDB
            pm_prompt: Системный промпт PM (для декомпозиции)
            all_tasks: Все задачи проекта (для обогащения input_data)

        Returns:
            True если задача успешно выполнена и прошла QA, иначе False.
        """
        project_id = self.orch.current_project.get("Id")
        task_db_id = task.get("Id")
        task_name = task.get("task_id")
        agent_name = task.get("agent_name")
        task_description = task.get("task_description")
        iteration_count = task.get("iteration_count", 0) or 0
        max_iter = task.get("max_iterations") or self.orch.MAX_TASK_ITERATIONS
        qa_feedback = task.get("qa_feedback", "")

        # Обогащаем input_data данными из зависимых задач
        if all_tasks is None:
            all_tasks = self.orch.tasks_db.get_tasks_by_project(project_id) if project_id else []
        input_data = self.orch._build_enriched_input_data(task, all_tasks)

        # client_hunter: перед LLM подмешиваем РЕАЛЬНЫЕ результаты Google (единственный источник)
        if agent_name == "client_hunter":
            input_data = self._inject_google_search(task, input_data)

        logger.info(f"📥 Входные данные для {agent_name}: {json.dumps(input_data, ensure_ascii=False)[:300]}")

        # Проверка лимита итераций
        if iteration_count >= max_iter:
            logger.warning(f"⚠️ Задача {task_name} превысила лимит итераций ({max_iter})")
            self.orch.tasks_db.update_task(
                task_db_id,
                {"status": "failed", "qa_feedback": "Превышен лимит итераций"},
            )
            send_telegram_alert(
                f"🚨 <b>Агент застрял в цикле</b>\n\n"
                f"Агент: <code>{agent_name}</code>\n"
                f"Задача: <code>{task_name}</code>\n"
                f"Итераций выполнено: {iteration_count}/{max_iter}\n\n"
                f"Требуется ручное вмешательство или декомпозиция задачи."
            )
            return False

        self.orch.tasks_db.update_task(
            task_db_id,
            {"status": "in_progress", "iteration_count": iteration_count + 1},
        )

        logger.info(
            f"▶ Выполнение задачи {task_name} ({agent_name}), "
            f"итерация {iteration_count + 1}/{max_iter}"
        )

        # Загружаем промпт агента (с кэшем)
        if agent_name not in self.agent_prompts_cache:
            self.agent_prompts_cache[agent_name] = load_prompt(agent_name)
        agent_prompt = self.agent_prompts_cache[agent_name]

        # Добавляем JSON Schema к промпту
        schema_prompt = self._build_schema_prompt(agent_prompt, agent_name)

        # Для developer: добавляем версию n8n из .env в контекст задачи
        if agent_name == "developer":
            input_data["n8n_version"] = Config.N8N_VERSION

        # Формируем задачу для агента
        agent_task = build_agent_task(task_description, input_data, qa_feedback, iteration_count)

        try:
            # Вызываем агента с Pydantic-валидацией
            validated_response, agent_tokens = self._call_with_validation(
                agent_name, schema_prompt, agent_task
            )

            # Обновляем бюджет токенов
            self.orch.current_project["tokens_used"] = (
                self.orch.current_project.get("tokens_used", 0) or 0
            ) + agent_tokens
            self.orch.projects_db.update_project(
                project_id, {"tokens_used": self.orch.current_project["tokens_used"]}
            )

            response_json = validated_response.model_dump_json(indent=2)

            # Валидация JSON перед сохранением (защита от обрезанных ответов)
            json_ok, response_json, json_feedback = validate_output_json(response_json, agent_name)
            if not json_ok:
                logger.error(f"❌ JSON от {agent_name} невалиден и не восстановлен: {json_feedback}")
                self.orch.tasks_db.update_task(
                    task_db_id,
                    {
                        "status": "pending" if iteration_count + 1 < max_iter else "failed",
                        "qa_feedback": json_feedback,
                        "tokens_used": (task.get("tokens_used", 0) or 0) + agent_tokens,
                    },
                )
                if iteration_count + 1 >= max_iter:
                    send_telegram_alert(
                        f"🚨 <b>Агент {agent_name} генерирует битый JSON</b>\n\n"
                        f"Задача: <code>{task_name}</code>\n"
                        f"Итераций: {iteration_count + 1}/{max_iter}\n"
                        f"Задача переведена в статус <b>failed</b>.\n\n"
                        f"Требуется ручное вмешательство или декомпозиция задачи."
                    )
                return False

            # Структурная валидация n8n workflow (защита от ошибки импорта
            # "X is not iterable" из-за неверных структур нод/версий)
            if agent_name == "developer":
                n8n_json = getattr(validated_response, "n8n_json", None)
                if isinstance(n8n_json, dict) and n8n_json.get("nodes"):
                    n8n_ok, n8n_issues = validate_n8n_workflow(n8n_json)
                    if not n8n_ok:
                        n8n_feedback = build_n8n_feedback(n8n_issues)
                        logger.error(
                            f"❌ n8n workflow от developer не пройдёт импорт "
                            f"({len(n8n_issues)} проблем): {task_name}"
                        )
                        self.orch.tasks_db.update_task(
                            task_db_id,
                            {
                                "status": "pending" if iteration_count + 1 < max_iter else "failed",
                                "qa_feedback": n8n_feedback,
                                "tokens_used": (task.get("tokens_used", 0) or 0) + agent_tokens,
                            },
                        )
                        if iteration_count + 1 >= max_iter:
                            send_telegram_alert(
                                f"🚨 <b>Developer генерирует невалидный n8n workflow</b>\n\n"
                                f"Задача: <code>{task_name}</code>\n"
                                f"Итераций: {iteration_count + 1}/{max_iter}\n"
                                f"Проблемы:\n{chr(10).join(n8n_issues[:5])}\n\n"
                                f"Задача переведена в статус <b>failed</b>."
                            )
                        return False

            log_to_agent_logs(
                project_id=project_id,
                agent_name=agent_name,
                status="review",
                task_description=(
                    f"[{task_name}] Итерация {iteration_count + 1}: {task_description[:150]}"
                ),
                full_response=response_json,
                tokens_used=agent_tokens,
            )

            self.orch.tasks_db.update_task(
                task_db_id,
                {
                    "output_data": response_json,
                    "tokens_used": (task.get("tokens_used", 0) or 0) + agent_tokens,
                },
            )

            # Диспатч на спец-обработчик или QA Gate
            # (вызываем через прокси-методы оркестратора — тесты могут их патчить)
            if agent_name == "architect":
                return self.orch._handle_architect(
                    task, task_db_id, task_name, validated_response, pm_prompt
                )
            if agent_name == "lead_hunter":
                return self.orch._handle_lead_hunter(
                    task, task_db_id, task_name, validated_response, pm_prompt
                )
            if agent_name == "client_hunter":
                return self.orch._handle_client_hunter(
                    task, task_db_id, task_name, validated_response, pm_prompt
                )
            if agent_name == "sales":
                return self.orch._handle_sales(
                    task, task_db_id, task_name, validated_response, pm_prompt
                )
            if agent_name == "analyst":
                return self.orch._handle_analyst(
                    task, task_db_id, task_name, validated_response, pm_prompt
                )

            # Стандартный путь: QA Gate для рабочих агентов
            if agent_name != "qa":
                return self.orch.run_qa_gate(
                    task, task_db_id, task_name, agent_name,
                    response_json, task_description, iteration_count, max_iter,
                )

            # QA выполнил свою задачу — просто завершаем
            self.orch.tasks_db.update_task(
                task_db_id, {"status": "completed", "qa_approved": "true"}
            )
            update_last_agent_log(project_id, agent_name, "completed")
            logger.info(f"✅ Задача QA {task_name} выполнена")
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка выполнения задачи {task_name}: {e}", exc_info=True)
            self.orch.tasks_db.update_task(
                task_db_id,
                {
                    "status": "pending" if iteration_count + 1 < max_iter else "failed",
                    "qa_feedback": f"Ошибка: {str(e)[:500]}",
                },
            )
            return False

    # ──────────────────────────────────────────────────────────────────────────
    # Вспомогательные методы
    # ──────────────────────────────────────────────────────────────────────────

    def _inject_google_search(self, task: Dict, input_data: Dict) -> Dict:
        """Подмешивает результаты Google Custom Search для client_hunter."""
        from .client_hunter_tools import build_search_queries, run_google_only_search

        goal = ""
        if self.orch.current_project:
            goal = self.orch.current_project.get("goal") or ""
        queries = build_search_queries(
            goal=goal,
            task_description=task.get("task_description") or "",
            llm_queries=input_data.get("search_queries")
            if isinstance(input_data.get("search_queries"), list)
            else None,
        )
        results = run_google_only_search(queries)
        input_data["google_search_results"] = results
        input_data["google_search_queries"] = queries
        input_data["search_source_policy"] = (
            "ONLY_GOOGLE_OPEN_SOURCES — запрещены Telegram, Avito, scrape, закрытые базы"
        )
        if not results:
            input_data["google_search_warning"] = (
                "Google не вернул результатов или GOOGLE_API_KEY/GOOGLE_CX не настроены. "
                "Не выдумывай клиентов — верни clients=[] и опиши причину в notes."
            )
        return input_data

    def _call_with_validation(self, agent_name: str, system_prompt: str, user_task: str):
        """Вызывает агента через call_and_parse_llm, выбирая модель по имени."""
        model_class = AGENT_MODELS.get(agent_name)
        if not model_class:
            raise ValueError(f"Нет Pydantic-модели для агента: {agent_name}")
        return call_and_parse_llm(
            call_llm_func=call_llm,
            agent_name=agent_name,
            system_prompt=system_prompt,
            user_task=user_task,
            response_model=model_class,
            max_retries=2,
        )

    @staticmethod
    def _build_schema_prompt(base_prompt: str, model_key: str) -> str:
        """Добавляет JSON Schema к базовому промпту.

        Если промпт уже содержит секцию «СТРОГАЯ СТРУКТУРА ОТВЕТА»
        (вшитую вручную), новая секция не добавляется — актуальная
        схема поддерживается синхронизацией самих .txt-файлов.
        Для промптов без встроенной схемы (pm_prompt, qa_prompt)
        секция генерируется динамически из Pydantic-модели.
        """
        model_class = AGENT_MODELS.get(model_key)
        if not model_class:
            return base_prompt

        # Предотвращаем дублирование: промпты агентов уже содержат схему
        if "СТРОГАЯ СТРУКТУРА ОТВЕТА" in base_prompt:
            return base_prompt

        schema = get_model_schema(model_class)
        return base_prompt + f"""

═══════════════════════════════════════════════════════════
СТРОГАЯ СТРУКТУРА ОТВЕТА (JSON Schema)
═══════════════════════════════════════════════════════════

Ты ДОЛЖЕН вернуть ТОЛЬКО валидный JSON, строго соответствующий этой схеме:

{schema}

ПРАВИЛА:
1. Верни ТОЛЬКО JSON, без комментариев до или после
2. Все обязательные поля должны быть заполнены
3. Типы данных должны точно соответствовать схеме (string, number, boolean, array)
4. Если поле Optional — можешь не включать его или установить null
5. Убедись, что все скобки и кавычки закрыты

═══════════════════════════════════════════════════════════
"""
