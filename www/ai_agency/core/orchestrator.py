"""
Orchestrator — главный класс, управляющий выполнением ИИ-агентства.
Реализует паттерн Supervisor-Workers с Pydantic-валидацией ответов LLM.

Логика разбита на специализированные классы:
  QAGate         — core/qa_gate.py
  AgentHandlers  — core/agent_handlers.py
  TaskExecutor   — core/task_executor.py

Здесь остаётся: инициализация, главный цикл, Task Graph, финализация,
разрешение тупиков и прокси-методы для обратной совместимости с тестами.
"""
import json
import logging
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from .agent_handlers import AgentHandlers
from .config import Config
from .nocodb import NocoDBClient, ProjectsClient, TasksClient
from .qa_gate import QAGate
from .schemas import (
    AGENT_MODELS,
    PMDeadlockResolution,
    PMFinalReport,
    PMTaskGraph,
    call_and_parse_llm,
    get_model_schema,
)
from .task_executor import TaskExecutor
from .utils import call_llm, load_prompt, log_to_agent_logs, update_last_agent_log

logger = logging.getLogger("Orchestrator")


class Orchestrator:
    """
    Оркестратор ИИ-агентства.

    Атрибуты:
        current_project:  Текущий проект из NocoDB
        tasks_db:         Клиент задач
        projects_db:      Клиент проектов
        agent_logs_db:    Клиент логов
        agency_running:   Флаг работы
        qa_gate:          QA Gate (выделенный класс)
        agent_handlers:   Спец-обработчики агентов
        task_executor:    Исполнитель задач
    """

    MAX_TOTAL_ITERATIONS = 50
    MAX_TASK_ITERATIONS = Config.MAX_TASK_ITERATIONS

    def __init__(self) -> None:
        self.agent_logs_db = NocoDBClient()
        self.projects_db = ProjectsClient()
        self.tasks_db = TasksClient()

        self.current_project: Optional[Dict[str, Any]] = None
        self.agency_running: bool = False

        # Субкомпоненты (держат ссылку на self, видят все актуальные атрибуты)
        self.qa_gate = QAGate(self)
        self.agent_handlers = AgentHandlers(self, self.qa_gate)
        self.task_executor = TaskExecutor(self)

        logger.info("🏗️ Orchestrator инициализирован")

    # ══════════════════════════════════════════════════════════════════════════
    # ИНИЦИАЛИЗАЦИЯ
    # ══════════════════════════════════════════════════════════════════════════

    def initialize(self, project_id: Optional[int] = None) -> bool:
        """Загружает активный проект из NocoDB или создаёт новый."""
        if project_id:
            self.current_project = self.projects_db.find_project_by_id(project_id)
            if not self.current_project:
                logger.error(f"❌ Проект {project_id} не найден")
                return False
        else:
            self.current_project = self.projects_db.find_project_by_status("in_progress")

            if not self.current_project:
                self.current_project = (
                    self.projects_db.find_project_by_status("stopped")
                    or self.projects_db.find_project_by_status("needs_human_review")
                )
                if self.current_project and self.current_project.get("Id"):
                    logger.info(f"▶ Возобновляем проект: {self.current_project.get('project_name')}")
                    self.current_project["status"] = "in_progress"
                    self.projects_db.update_project(
                        self.current_project["Id"], {"status": "in_progress"}
                    )
                else:
                    logger.info("▶ Создаём новый проект")
                    self.current_project = self.projects_db.create_project(
                        project_name=Config.DEFAULT_PROJECT_NAME,
                        client_name=Config.DEFAULT_CLIENT_NAME,
                        goal=Config.DEFAULT_GOAL,
                        token_budget=Config.TOKEN_BUDGET,
                    )

        if not self.current_project or not self.current_project.get("Id"):
            logger.error("❌ Не удалось получить/создать проект")
            return False

        # Гарантируем наличие обязательных полей
        self.current_project.setdefault("project_name", Config.DEFAULT_PROJECT_NAME)
        self.current_project.setdefault("client_name", Config.DEFAULT_CLIENT_NAME)
        self.current_project.setdefault("status", "in_progress")

        logger.info(
            f"✅ Проект инициализирован: {self.current_project.get('project_name')} "
            f"(ID: {self.current_project.get('Id')})"
        )
        return True

    # ══════════════════════════════════════════════════════════════════════════
    # ГЛАВНЫЙ ЦИКЛ
    # ══════════════════════════════════════════════════════════════════════════

    def run(self) -> None:
        """Главный цикл Supervisor-Workers."""
        self.agency_running = True
        pm_prompt = load_prompt("pm")
        iteration = 0

        logger.info("▶ Запуск Supervisor-Workers оркестрации")

        while self.agency_running and iteration < self.MAX_TOTAL_ITERATIONS:
            iteration += 1

            if not self.current_project or not self.current_project.get("Id"):
                logger.error("❌ Нет активного проекта")
                break

            project_id = self.current_project.get("Id")
            tokens_used = self.current_project.get("tokens_used", 0) or 0
            token_budget = self.current_project.get("token_budget", Config.TOKEN_BUDGET) or Config.TOKEN_BUDGET

            # ── Проверка бюджета токенов ────────────────────────────────────
            remaining = token_budget - tokens_used
            if remaining < token_budget * 0.2:
                logger.warning(f"⚠️ Бюджет на исходе: {tokens_used}/{token_budget}")
                self.current_project["status"] = "needs_human_review"
                self.projects_db.update_project(
                    project_id,
                    {"status": "needs_human_review", "tokens_used": tokens_used},
                )
                log_to_agent_logs(
                    project_id=project_id,
                    agent_name="PM",
                    status="needs_review",
                    task_description=(
                        f"Автоматическая остановка: "
                        f"использовано {tokens_used} из {token_budget} токенов."
                    ),
                    full_response=json.dumps(
                        {"reason": "budget_exhausted", "tokens_used": tokens_used,
                         "token_budget": token_budget},
                        ensure_ascii=False,
                    ),
                    tokens_used=0,
                )
                break

            # ── Создание Task Graph ─────────────────────────────────────────
            if not self._create_initial_task_graph():
                break

            # ── Чтение задач ────────────────────────────────────────────────
            tasks = self.tasks_db.get_tasks_by_project(project_id)
            self.check_and_complete_parent_tasks(tasks)
            tasks = self.tasks_db.get_tasks_by_project(project_id)

            completed = [t for t in tasks if t.get("status") == "completed"]
            failed    = [t for t in tasks if t.get("status") == "failed"]
            pending   = [t for t in tasks if t.get("status") == "pending"]
            in_prog   = [t for t in tasks if t.get("status") == "in_progress"]

            logger.info(
                f"📊 Статус: completed={len(completed)}, pending={len(pending)}, "
                f"in_progress={len(in_prog)}, failed={len(failed)}"
            )

            # ── Все задачи завершены → финализация ─────────────────────────
            if not pending and not in_prog and completed:
                logger.info(f"🏁 Все задачи завершены: {len(completed)} выполнено")
                self.finalize(pm_prompt, tasks)
                break

            if not pending and not in_prog:
                logger.warning("⚠️ Нет задач для выполнения")
                break

            # ── Поиск готовых задач ─────────────────────────────────────────
            completed_ids = self._expand_completed_with_parents(
                tasks, [t.get("task_id") for t in completed]
            )
            ready_tasks = self._find_ready_tasks(pending, completed_ids)
            logger.info(f"🎯 Готовых задач: {len(ready_tasks)}")

            # ── Обработка застрявших задач (>5 мин в in_progress) ───────────
            if not ready_tasks and in_prog:
                stuck = self._find_stuck_tasks(in_prog)
                if stuck:
                    logger.warning(f"⚠️ Найдены застрявшие задачи: {[t.get('task_id') for t in stuck]}")
                    for t in stuck:
                        self.tasks_db.update_task(
                            t.get("Id"),
                            {"status": "pending", "qa_feedback": "Задача застряла, возврат в pending"},
                        )
                    continue
                logger.info("⏳ Ждём завершения текущих задач...")
                time.sleep(5)
                continue

            # ── Тупик ───────────────────────────────────────────────────────
            if not ready_tasks:
                if pending:
                    logger.warning(f"⚠️ Тупик: {len(pending)} задач в pending")
                    if not self.resolve_deadlock(pm_prompt, tasks, pending):
                        logger.error("❌ PM не смог разрешить тупик")
                        break
                else:
                    logger.warning("⚠️ Нет готовых задач — тупик")
                    break
                continue

            # ── Выполняем первую готовую задачу ─────────────────────────────
            self.execute_task(ready_tasks[0], pm_prompt, all_tasks=tasks)

        self.agency_running = False
        logger.info(
            f"📊 ИТОГ: статус={self.current_project.get('status')}, "
            f"токенов={self.current_project.get('tokens_used')}"
        )

    def stop(self) -> None:
        """Останавливает оркестратор."""
        self.agency_running = False
        if self.current_project and self.current_project.get("Id"):
            self.current_project["status"] = "stopped"
            self.projects_db.update_project(self.current_project["Id"], {"status": "stopped"})
        logger.info("⏹ Оркестратор остановлен")

    # ══════════════════════════════════════════════════════════════════════════
    # СОЗДАНИЕ TASK GRAPH
    # ══════════════════════════════════════════════════════════════════════════

    def _create_initial_task_graph(self) -> bool:
        """Создаёт начальный Task Graph через PM (только если задач ещё нет)."""
        project_id = self.current_project.get("Id")
        if self.tasks_db.get_tasks_by_project(project_id):
            return True  # Task Graph уже существует

        logger.info("🧠 PM строит Task Graph...")
        pm_prompt = load_prompt("pm")

        project_goal = self.current_project.get("goal", "")
        project_name = self.current_project.get("project_name", "")

        task_graph_prompt = f"""
Ты — Project Manager. Проанализируй цели проекта и построй оптимальный Task Graph.

ПРОЕКТ:
Название: {project_name}
Цель: {project_goal}

ТВОЯ ЗАДАЧА:
1. Внимательно проанализируй цель проекта
2. Определи, какие агенты нужны для достижения цели
3. Если в цели указано "не делать [задача]" — исключи соответствующего агента
4. Построй оптимальный граф задач только с нужными агентами

ДОСТУПНЫЕ АГЕНТЫ:
- lead_hunter: поиск потенциальных клиентов (селлеров WB/Ozon)
- sales: написание холодных сообщений и квалификация лидов
- analyst: расчёт ROI и подготовка презентации
- architect: проектирование архитектуры интеграции
- developer: разработка автоматизаций (n8n, Albato)
- crm_customizer: настройка CRM (Битрикс24, AmoCRM, Bpium)
- qa: тестирование и валидация
- tech_writer: создание документации для клиента

⚠️ КРИТИЧНО: Используй поле "task_id" (НЕ "id"!) для идентификации задач!

ФОРМАТ ОТВЕТА (строго JSON, без markdown-обёрток):
{{
  "tasks": [
    {{
      "task_id": "task_001",
      "agent_name": "имя_агента",
      "task_description": "описание задачи",
      "depends_on": [],
      "input_data": {{}},
      "max_iterations": 3
    }}
  ],
  "excluded_agents": [],
  "reasoning": "Все агенты нужны"
}}

ВАЖНО:
- Верни ТОЛЬКО JSON, без ```json ... ``` обёрток
- QA всегда нужен, если есть хотя бы одна задача
"""

        schema_prompt = self.build_prompt_with_schema(pm_prompt, "pm_task_graph")

        try:
            pm_task_graph, pm_tokens = self.call_agent_with_validation(
                "PM", schema_prompt, task_graph_prompt, "pm_task_graph", max_retries=3
            )

            self.current_project["tokens_used"] = (
                self.current_project.get("tokens_used", 0) or 0
            ) + pm_tokens
            self.projects_db.update_project(
                project_id, {"tokens_used": self.current_project["tokens_used"]}
            )

            tasks_list = pm_task_graph.tasks
            excluded_agents = getattr(pm_task_graph, "excluded_agents", [])
            reasoning = getattr(pm_task_graph, "reasoning", "")

            if not tasks_list:
                logger.error("❌ PM не вернул задачи")
                return False

            if excluded_agents:
                logger.info(f"📋 PM исключил агентов: {excluded_agents} | Причина: {reasoning}")

            project_context = json.dumps(
                {
                    "project_goal": self.current_project.get("goal", ""),
                    "client_name":  self.current_project.get("client_name", ""),
                    "project_name": self.current_project.get("project_name", ""),
                },
                ensure_ascii=False,
            )

            VALID_AGENTS = {
                "lead_hunter", "sales", "analyst", "architect",
                "developer", "crm_customizer", "qa", "tech_writer",
            }

            for task_data in tasks_list:
                if "task_id" not in task_data:
                    logger.warning(f"⚠️ Задача без task_id, пропускаем: {task_data}")
                    continue

                # Защита: agent_name должен быть строкой из допустимого набора
                raw_agent = task_data.get("agent_name", "")
                if not isinstance(raw_agent, str) or raw_agent not in VALID_AGENTS:
                    logger.error(
                        f"❌ Задача {task_data.get('task_id')} содержит "
                        f"недопустимый agent_name={repr(str(raw_agent)[:80])}. Пропускаем."
                    )
                    continue

                task_data.update({
                    "project_id": project_id,
                    "status": "pending",
                    "iteration_count": 0,
                    "qa_approved": "pending",
                    "created_at": datetime.now().isoformat(),
                })
                if not task_data.get("input_data") or task_data.get("input_data") == "{}":
                    task_data["input_data"] = project_context
                self.tasks_db.create_task(task_data)

            self.projects_db.update_project(
                project_id,
                {
                    "plan": pm_task_graph.model_dump_json(indent=2),
                    "excluded_agents": json.dumps(excluded_agents, ensure_ascii=False),
                    "reasoning": reasoning,
                },
            )

            logger.info(f"✅ Task Graph создан: {len(tasks_list)} задач")
            log_to_agent_logs(
                project_id=project_id,
                agent_name="PM",
                status="completed",
                task_description=(
                    f"Task Graph: {len(tasks_list)} задач. "
                    f"Исключено агентов: {len(excluded_agents)}. {reasoning}"
                ),
                full_response=pm_task_graph.model_dump_json(indent=2),
                tokens_used=0,
            )
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка построения Task Graph: {e}", exc_info=True)
            return False

    # ══════════════════════════════════════════════════════════════════════════
    # ФИНАЛИЗАЦИЯ
    # ══════════════════════════════════════════════════════════════════════════

    def finalize(self, pm_prompt: str, tasks: List[Dict[str, Any]]) -> bool:
        """PM формирует финальный отчёт для клиента."""
        project_id = self.current_project.get("Id")

        task_results = [
            {
                "task_id":     t.get("task_id"),
                "agent":       t.get("agent_name"),
                "description": t.get("task_description"),
                "output":      (t.get("output_data") or "")[:500],
                "status":      t.get("status"),
                "iterations":  t.get("iteration_count") or 0,
                "tokens":      t.get("tokens_used") or 0,
            }
            for t in tasks
        ]

        total_tokens     = sum(t.get("tokens_used") or 0 for t in tasks)
        total_iterations = sum(t.get("iteration_count") or 0 for t in tasks)
        completed_count  = sum(1 for t in tasks if t.get("status") == "completed")
        tokens_per_agent = {}
        for t in tasks:
            agent = t.get("agent_name")
            tokens_per_agent[agent] = tokens_per_agent.get(agent, 0) + (t.get("tokens_used") or 0)

        schema_prompt = self.build_prompt_with_schema(load_prompt("pm"), "pm_final_report")

        user_task = f"""
ПРОЕКТ ЗАВЕРШЕН. Все задачи выполнены.

ИНФОРМАЦИЯ О ПРОЕКТЕ:
Клиент: {self.current_project.get('client_name')}
Цель: {self.current_project.get('goal')}

ВЫПОЛНЕННЫЕ ЗАДАЧИ ({completed_count} из {len(tasks)}):
{json.dumps(task_results, ensure_ascii=False, indent=2)}

МЕТРИКИ:
- Общие затраты токенов: {total_tokens}
- Общее количество итераций: {total_iterations}
- Затраты по агентам: {json.dumps(tokens_per_agent, ensure_ascii=False)}

СФОРМИРУЙ финальный отчёт для клиента согласно схеме выше.
Отчёт в markdown-формате, понятный не-техническому специалисту.
"""

        try:
            pm_final_report, pm_tokens = self.call_agent_with_validation(
                "PM", schema_prompt, user_task, "pm_final_report"
            )

            self.current_project["tokens_used"] = (
                self.current_project.get("tokens_used", 0) or 0
            ) + pm_tokens
            self.projects_db.update_project(
                project_id, {"tokens_used": self.current_project["tokens_used"]}
            )

            final_report = pm_final_report.final_report
            metrics_json = json.dumps(pm_final_report.metrics, ensure_ascii=False)
            completed_at = datetime.now().isoformat()

            self.current_project.update({
                "final_report": final_report,
                "metrics": metrics_json,
                "completed_at": completed_at,
                "status": "completed",
            })
            self.projects_db.update_project(
                project_id,
                {
                    "status": "completed",
                    "final_report": final_report,
                    "metrics": metrics_json,
                    "completed_at": completed_at,
                },
            )

            logger.info("✅ Проект завершён. Финальный отчёт сохранён.")
            log_to_agent_logs(
                project_id=project_id,
                agent_name="PM",
                status="completed",
                task_description="Финализация проекта. Отчёт подготовлен для клиента.",
                full_response=pm_final_report.model_dump_json(indent=2),
                tokens_used=pm_tokens,
            )
            return True

        except Exception as e:
            logger.error(f"❌ Ошибка финализации: {e}", exc_info=True)
            self.current_project["status"] = "needs_human_review"
            self.projects_db.update_project(project_id, {"status": "needs_human_review"})
            return False

    # ══════════════════════════════════════════════════════════════════════════
    # РАЗРЕШЕНИЕ ТУПИКОВ
    # ══════════════════════════════════════════════════════════════════════════

    def resolve_deadlock(
        self,
        pm_prompt: str,
        tasks: List[Dict[str, Any]],
        pending_tasks: List[Dict[str, Any]],
    ) -> bool:
        """Вызывает PM для разрешения тупика с Pydantic-валидацией."""
        project_id = self.current_project.get("Id")

        tasks_summary = [
            {
                "task_id":    t.get("task_id"),
                "agent":      t.get("agent_name"),
                "status":     t.get("status"),
                "depends_on": t.get("depends_on", "[]"),
            }
            for t in tasks
        ]
        pending_summary = [
            {
                "task_id":    t.get("task_id"),
                "agent":      t.get("agent_name"),
                "depends_on": t.get("depends_on"),
            }
            for t in pending_tasks
        ]

        schema_prompt = self.build_prompt_with_schema(load_prompt("pm"), "pm_deadlock")
        user_task = f"""
ПРОЕКТ В ТУПИКЕ! Есть задачи в статусе pending, но ни одна не готова к выполнению.

ВСЕ ЗАДАЧИ ПРОЕКТА:
{json.dumps(tasks_summary, ensure_ascii=False, indent=2)}

ЗАДАЧИ В PENDING:
{json.dumps(pending_summary, ensure_ascii=False, indent=2)}

ПРЕДЛОЖИ решение согласно схеме выше.
Если не можешь разрешить тупик, установи solution="stop_project".
"""

        try:
            pm_deadlock, pm_tokens = self.call_agent_with_validation(
                "PM", schema_prompt, user_task, "pm_deadlock", max_retries=3
            )

            self.current_project["tokens_used"] = (
                self.current_project.get("tokens_used", 0) or 0
            ) + pm_tokens
            self.projects_db.update_project(
                project_id, {"tokens_used": self.current_project["tokens_used"]}
            )

            logger.info(
                f"💡 PM: {pm_deadlock.solution} | {pm_deadlock.comment}"
            )

            for action in pm_deadlock.actions:
                action_dict = action.model_dump() if hasattr(action, "model_dump") else action
                action_type = action_dict.get("action")
                task_id     = action_dict.get("task_id")

                task = next((t for t in tasks if t.get("task_id") == task_id), None)
                if not task:
                    continue
                task_db_id = task.get("Id")

                if action_type == "update_task":
                    update_data = {}
                    if "new_status" in action_dict:
                        update_data["status"] = action_dict["new_status"]
                    if "new_depends_on" in action_dict:
                        update_data["depends_on"] = json.dumps(
                            action_dict["new_depends_on"], ensure_ascii=False
                        )
                    if update_data:
                        self.tasks_db.update_task(task_db_id, update_data)

                elif action_type == "skip_task":
                    self.tasks_db.update_task(
                        task_db_id,
                        {"status": "failed", "qa_feedback": "Пропущено по решению PM"},
                    )

            # need_human_review и stop_project — оба останавливают цикл.
            # need_human_review: выставляем статус проекта, чтобы дашборд показал ручное вмешательство.
            # Если вернуть True при need_human_review — цикл продолжается, тупик повторяется бесконечно.
            if pm_deadlock.solution == "need_human_review":
                logger.warning("🛑 PM решил: need_human_review — останавливаем цикл")
                self.projects_db.update_project(
                    project_id,
                    {"status": "needs_human_review"},
                )
                self.current_project["status"] = "needs_human_review"
                return False

            if pm_deadlock.solution == "stop_project":
                logger.warning("🛑 PM решил: stop_project — останавливаем цикл")
                self.projects_db.update_project(
                    project_id,
                    {"status": "stopped"},
                )
                self.current_project["status"] = "stopped"
                return False

            return True

        except Exception as e:
            logger.error(f"❌ Ошибка разрешения тупика: {e}", exc_info=True)
            return False

    # ══════════════════════════════════════════════════════════════════════════
    # ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ
    # ══════════════════════════════════════════════════════════════════════════

    def call_agent_with_validation(
        self,
        agent_name: str,
        system_prompt: str,
        user_task: str,
        model_key: str,
        max_retries: int = 2,
    ):
        """Тонкая обёртка: call_and_parse_llm с поиском модели по ключу."""
        model_class = AGENT_MODELS.get(model_key)
        if not model_class:
            raise ValueError(f"Неизвестная модель: {model_key}")
        return call_and_parse_llm(
            call_llm_func=call_llm,
            agent_name=agent_name,
            system_prompt=system_prompt,
            user_task=user_task,
            response_model=model_class,
            max_retries=max_retries,
        )

    def build_prompt_with_schema(self, base_prompt: str, model_key: str) -> str:
        """Добавляет JSON Schema к промпту (ищет модель по строковому ключу)."""
        model_class = AGENT_MODELS.get(model_key)
        if not model_class:
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

    @staticmethod
    def _find_developer_placeholder(tasks: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Возвращает placeholder developer-задачу из initial task graph.

        Placeholder — это задача с agent_name=developer, task_id НЕ начинающимся с "dev_".
        Создаётся PM в initial task graph (например task_003) и позже заменяется dev_* подзадачами.
        """
        for t in tasks:
            tid = t.get("task_id", "")
            if t.get("agent_name") == "developer" and not tid.startswith("dev_"):
                return t
        return None

    def check_and_complete_parent_tasks(self, tasks: List[Dict[str, Any]]) -> None:
        """Если все dev_* подзадачи завершены — помечает placeholder developer-задачу как completed.

        Ранее метод был жёстко привязан к "task_003". Теперь placeholder ищется динамически:
        agent_name=developer, task_id не начинается с "dev_".
        """
        dev_subtasks = [t for t in tasks if t.get("task_id", "").startswith("dev_")]
        if not dev_subtasks:
            return
        if not all(t.get("status") == "completed" for t in dev_subtasks):
            return

        parent_task = self._find_developer_placeholder(tasks)
        if parent_task and parent_task.get("status") != "completed":
            parent_id = parent_task.get("task_id")
            logger.info(f"✅ Все {len(dev_subtasks)} dev_* подзадач завершены. Помечаем '{parent_id}' как completed.")
            self.tasks_db.update_task(
                parent_task.get("Id"),
                {
                    "status": "completed",
                    "qa_approved": "true",
                    "qa_feedback": f"Все {len(dev_subtasks)} подзадач завершены успешно",
                },
            )

    def _expand_completed_with_parents(
        self, tasks: List[Dict[str, Any]], completed_ids: List[str]
    ) -> List[str]:
        """Добавляет placeholder developer-задачу в completed_ids, если все dev_* завершены.

        Используется в _find_ready_tasks для разблокировки qa/tech_writer ДО того, как
        check_and_complete_parent_tasks успеет записать completed в БД (оба вызова в одном цикле).
        Ранее было жёстко задано "task_003" — теперь ищется динамически.
        """
        expanded = set(completed_ids)
        dev_statuses = [
            t.get("status", "")
            for t in tasks
            if t.get("task_id", "").startswith("dev_")
        ]
        if dev_statuses and all(s == "completed" for s in dev_statuses):
            placeholder = self._find_developer_placeholder(tasks)
            if placeholder:
                expanded.add(placeholder.get("task_id"))
        return list(expanded)

    def _find_ready_tasks(
        self, pending_tasks: List[Dict[str, Any]], completed_ids: List[str]
    ) -> List[Dict[str, Any]]:
        """Находит задачи, у которых все зависимости выполнены."""
        ready = []
        for task in pending_tasks:
            depends_on = task.get("depends_on", "[]")
            try:
                depends_on = json.loads(depends_on) if isinstance(depends_on, str) else depends_on
            except Exception:
                depends_on = []
            if all(dep_id in completed_ids for dep_id in depends_on):
                ready.append(task)
        return ready

    def _build_enriched_input_data(
        self, task: Dict[str, Any], all_tasks: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Обогащает input_data задачи:
          - контекст проекта (goal, client_name, project_name)
          - output_data зависимых задач
          - накопленные контексты (leads, sales, analyst)
        """
        raw = task.get("input_data", "{}")
        try:
            input_data = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            input_data = {}

        depends_on = task.get("depends_on", "[]")
        try:
            depends_on = json.loads(depends_on) if isinstance(depends_on, str) else depends_on
        except Exception:
            depends_on = []

        # Контекст проекта
        if self.current_project:
            for key, field in (
                ("project_goal", "goal"),
                ("client_name", "client_name"),
                ("project_name", "project_name"),
            ):
                if key not in input_data:
                    input_data[key] = self.current_project.get(field, "")
            for ctx_key in ("leads_context", "sales_context", "analyst_context"):
                if ctx_key in self.current_project:
                    input_data[ctx_key] = self.current_project[ctx_key]

        if not depends_on:
            return input_data

        # Output зависимых задач
        dependency_data = {}
        for dep_id in depends_on:
            dep_task = next((t for t in all_tasks if t.get("task_id") == dep_id), None)
            if dep_task and dep_task.get("output_data"):
                raw_out = dep_task.get("output_data", "")
                try:
                    parsed = json.loads(raw_out) if isinstance(raw_out, str) else raw_out
                except json.JSONDecodeError:
                    parsed = raw_out
                dependency_data[dep_id] = {
                    "agent_name":       dep_task.get("agent_name"),
                    "task_description": dep_task.get("task_description"),
                    "output":           parsed,
                }

        if dependency_data:
            input_data["dependency_outputs"] = dependency_data
            # Для QA: плоские ссылки на каждый output
            if task.get("agent_name") == "qa":
                for dep_id, dep_data in dependency_data.items():
                    input_data[f"output_from_{dep_data.get('agent_name', 'unknown')}"] = (
                        dep_data.get("output")
                    )

        return input_data

    @staticmethod
    def _find_stuck_tasks(in_progress: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Возвращает задачи, застрявшие в in_progress более 5 минут."""
        from datetime import datetime, timezone

        stuck = []
        for t in in_progress:
            updated_at = t.get("updated_at")
            if not updated_at:
                continue
            try:
                last_update = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                if (datetime.now(timezone.utc) - last_update).total_seconds() > 300:
                    stuck.append(t)
            except Exception:
                pass
        return stuck

    # ══════════════════════════════════════════════════════════════════════════
    # ПРОКСИ-МЕТОДЫ (обратная совместимость с тестами и main.py)
    # ══════════════════════════════════════════════════════════════════════════

    def execute_task(
        self,
        task: Dict[str, Any],
        pm_prompt: str,
        all_tasks: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """Делегирует выполнение задачи TaskExecutor."""
        return self.task_executor.execute(task, pm_prompt, all_tasks)

    def run_qa_gate(
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
        """Делегирует QA-проверку QAGate."""
        return self.qa_gate.run(
            task, task_db_id, task_name, agent_name,
            agent_response, task_description,
            iteration_count, max_iter, update_status,
        )

    def _handle_architect(self, task, task_db_id, task_name, agent_response, pm_prompt) -> bool:
        return self.agent_handlers.handle_architect(
            task, task_db_id, task_name, agent_response, pm_prompt
        )

    def _handle_lead_hunter(self, task, task_db_id, task_name, agent_response, pm_prompt) -> bool:
        return self.agent_handlers.handle_lead_hunter(
            task, task_db_id, task_name, agent_response, pm_prompt
        )

    # Псевдоним для обратной совместимости со старым кодом
    _handle_lead_hunter_with_tools = _handle_lead_hunter

    def _handle_sales(self, task, task_db_id, task_name, agent_response, pm_prompt) -> bool:
        return self.agent_handlers.handle_sales(
            task, task_db_id, task_name, agent_response, pm_prompt
        )

    def _handle_analyst(self, task, task_db_id, task_name, agent_response, pm_prompt) -> bool:
        return self.agent_handlers.handle_analyst(
            task, task_db_id, task_name, agent_response, pm_prompt
        )
