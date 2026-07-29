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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import ValidationError

from .agent_handlers import AgentHandlers
from .config import Config
from .nocodb import NocoDBClient, ProjectsClient, TasksClient
from .parallel_wave import select_parallel_wave
from .project_iteration import (
    VALID_AGENTS,
    build_previous_tasks_digest,
    enrich_task_input,
    get_project_iteration,
    normalize_iteration_task_ids,
    parse_metrics,
    prepare_iteration_metrics,
)
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
        # Параллельные волны задач (Фаза 3.1): токены / контекст / кэш промптов
        self.state_lock = threading.RLock()

        # Субкомпоненты (держат ссылку на self, видят все актуальные атрибуты)
        self.qa_gate = QAGate(self)
        self.agent_handlers = AgentHandlers(self, self.qa_gate)
        self.task_executor = TaskExecutor(self)

        logger.info("🏗️ Orchestrator инициализирован")

    def add_tokens(self, delta: int, *, persist: bool = True) -> int:
        """Thread-safe инкремент tokens_used текущего проекта."""
        if not self.current_project:
            return 0
        with self.state_lock:
            total = (self.current_project.get("tokens_used") or 0) + (delta or 0)
            self.current_project["tokens_used"] = total
            project_id = self.current_project.get("Id")
        if persist and project_id is not None:
            self.projects_db.update_project(project_id, {"tokens_used": total})
        return total

    # ══════════════════════════════════════════════════════════════════════════
    # ИНИЦИАЛИЗАЦИЯ
    # ══════════════════════════════════════════════════════════════════════════

    def initialize(
        self,
        project_id: Optional[int] = None,
        *,
        create: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """Загружает активный проект из NocoDB или создаёт новый.

        Args:
            project_id: загрузить конкретный проект по Id.
            create: словарь полей нового проекта
                {project_name, client_name, goal, token_budget?, current_phase?}.
                Если передан — всегда создаётся новый проект (предыдущий in_progress
                переводится в stopped).
        """
        if create:
            name = (create.get("project_name") or "").strip()
            client = (create.get("client_name") or "").strip()
            goal = (create.get("goal") or "").strip()
            if not name or not client or not goal:
                logger.error("❌ create: нужны project_name, client_name, goal")
                return False
            budget = create.get("token_budget") or Config.TOKEN_BUDGET
            phase = create.get("current_phase") or "lead_gen"

            # Освобождаем текущий активный проект, чтобы не путать статусы
            try:
                active = self.projects_db.find_project_by_status("in_progress")
                if active and active.get("Id"):
                    self.projects_db.update_project(
                        active["Id"], {"status": "stopped"}
                    )
                    logger.info(
                        f"⏸ Предыдущий проект #{active['Id']} "
                        f"({active.get('project_name')}) → stopped"
                    )
            except Exception as e:
                logger.warning(f"⚠️ Не удалось остановить предыдущий проект: {e}")

            logger.info(f"▶ Создаём новый проект из формы: {name}")
            self.current_project = self.projects_db.create_project(
                project_name=name,
                client_name=client,
                goal=goal,
                token_budget=int(budget),
                current_phase=phase,
            )
        elif project_id:
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

            # ── Выполняем волну готовых независимых задач (Фаза 3.1) ─────────
            self._execute_ready_wave(ready_tasks, pm_prompt, all_tasks=tasks)

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
- client_hunter: монетизация — поиск клиентов ТОЛЬКО через Google + персональное УТП
- lead_hunter: поиск потенциальных клиентов (селлеров WB/Ozon)
- sales: написание холодных сообщений и квалификация лидов (ОБЯЗАТЕЛЕН после hunter)
- analyst: расчёт ROI и подготовка презентации
- architect: проектирование архитектуры интеграции
- developer: разработка автоматизаций (n8n, Albato)
- crm_customizer: настройка CRM (Битрикс24, AmoCRM, Bpium)
- qa: тестирование и валидация
- tech_writer: создание документации для клиента

⚠️ КРИТИЧНО: Используй поле "task_id" (НЕ "id"!) для идентификации задач!

ОБЯЗАТЕЛЬНАЯ ЦЕПОЧКА МОНЕТИЗАЦИИ:
Выбери ОДИН hunter (не оба сразу):
- client_hunter — общий поиск клиентов по ICP (клиники, школы, ИМ, …) через OpenSERP;
- lead_hunter — селлеры/бренды WB/Ozon (тоже OpenSERP, без выдуманных контактов).
Если в графе есть hunter — ОБЯЗАТЕЛЬНО sales с depends_on на него.
Пример: task_001 client_hunter → task_002 sales (depends_on: ["task_001"]).
ЗАПРЕЩЕНО одновременно ставить client_hunter и lead_hunter.

max_iterations по умолчанию: 3 для большинства агентов, но для agent_name="developer"
используй 6 — сложные n8n-интеграции чаще требуют доработки по фидбеку QA.

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
- client_hunter/lead_hunter без sales — ЗАПРЕЩЕНО
"""

        schema_prompt = self.build_prompt_with_schema(pm_prompt, "pm_task_graph")

        try:
            pm_task_graph, pm_tokens = self.call_agent_with_validation(
                "PM", schema_prompt, task_graph_prompt, "pm_task_graph", max_retries=3
            )

            self.add_tokens(pm_tokens)

            tasks_list = list(pm_task_graph.tasks or [])
            excluded_agents = list(getattr(pm_task_graph, "excluded_agents", []) or [])
            reasoning = getattr(pm_task_graph, "reasoning", "")

            if not tasks_list:
                logger.error("❌ PM не вернул задачи")
                return False

            from .task_graph_rules import enforce_sales_after_hunters, prefer_single_hunter

            tasks_list, excluded_agents = prefer_single_hunter(
                tasks_list,
                excluded_agents,
                goal=project_goal,
            )
            tasks_list, excluded_agents = enforce_sales_after_hunters(
                tasks_list, excluded_agents
            )

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
                "client_hunter", "lead_hunter", "sales", "analyst", "architect",
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
    # ИТЕРАЦИЯ ПРОЕКТА (замечания человека → PM-replan)
    # ══════════════════════════════════════════════════════════════════════════

    def start_project_iteration(self, human_remarks: str) -> Dict[str, Any]:
        """Новая итерация на том же project_id: PM строит граф доработки.

        Старые задачи и final_report сохраняются (отчёт — в metrics.iteration_history).
        Новые task_id получают префикс iterN_.
        """
        remarks = (human_remarks or "").strip()
        if not remarks:
            raise ValueError("Нужны замечания человека для новой итерации")
        if not self.current_project or not self.current_project.get("Id"):
            raise ValueError("Нет загруженного проекта")
        if self.agency_running:
            raise ValueError("Оркестратор уже запущен — сначала остановите")

        project_id = self.current_project["Id"]
        current_iter = get_project_iteration(self.current_project)
        next_iter = current_iter + 1
        existing_tasks = self.tasks_db.get_tasks_by_project(project_id)
        digest = build_previous_tasks_digest(existing_tasks)
        previous_by_id = {
            str(t.get("task_id")): t for t in existing_tasks if t.get("task_id")
        }

        metrics, next_iter = prepare_iteration_metrics(
            self.current_project,
            next_iteration=next_iter,
            human_remarks=remarks,
        )

        logger.info(
            "🔁 Итерация проекта #%s: %s → %s",
            project_id, current_iter, next_iter,
        )

        pm_prompt = load_prompt("pm")
        schema_prompt = self.build_prompt_with_schema(pm_prompt, "pm_task_graph")
        prev_report = (self.current_project.get("final_report") or "")[:8000]

        replan_prompt = f"""
Ты — Project Manager. Человек запросил НОВУЮ ИТЕРАЦИЮ доработки проекта.

ПРОЕКТ:
Название: {self.current_project.get('project_name')}
Клиент: {self.current_project.get('client_name')}
Цель: {self.current_project.get('goal')}
Текущая итерация (завершённая): {current_iter}
Новая итерация: {next_iter}

ФИНАЛЬНЫЙ ОТЧЁТ ПРЕДЫДУЩЕЙ ИТЕРАЦИИ (фрагмент):
{prev_report or '(отчёта нет)'}

ДАЙДЖЕСТ ЗАДАЧ ПРЕДЫДУЩИХ ИТЕРАЦИЙ:
{json.dumps(digest, ensure_ascii=False, indent=2)[:14000]}

ЗАМЕЧАНИЯ ЧЕЛОВЕКА (обязательно учесть):
{remarks}

ТВОЯ ЗАДАЧА:
1. Проанализируй замечания относительно уже сделанного.
2. Построй НОВЫЙ Task Graph ТОЛЬКО с задачами, нужными для доработки.
   Не повторяй работу, которой человек доволен.
3. task_id начинай с префикса iter{next_iter}_ (например iter{next_iter}_task_001).
4. depends_on — ТОЛЬКО между новыми задачами этого графа.
   Чтобы опереться на результат прошлой задачи, укажи в input_data:
   "previous_task_ids": ["task_001", ...]
5. Обычно нужны: developer и/или architect (если меняется архитектура),
   qa, tech_writer (обновить документацию). Не добавляй hunter/sales без нужды.
6. Для developer max_iterations=6, для остальных 3.

ФОРМАТ — строго JSON pm_task_graph (tasks, excluded_agents, reasoning).
"""

        pm_task_graph, pm_tokens = self.call_agent_with_validation(
            "PM", schema_prompt, replan_prompt, "pm_task_graph", max_retries=3
        )

        self.add_tokens(pm_tokens, persist=False)

        tasks_list = list(pm_task_graph.tasks or [])
        excluded_agents = list(getattr(pm_task_graph, "excluded_agents", []) or [])
        reasoning = getattr(pm_task_graph, "reasoning", "") or ""

        if not tasks_list:
            raise RuntimeError("PM не вернул задачи для итерации")

        from .task_graph_rules import enforce_sales_after_hunters, prefer_single_hunter

        tasks_list, excluded_agents = prefer_single_hunter(
            tasks_list, excluded_agents, goal=self.current_project.get("goal", "")
        )
        tasks_list, excluded_agents = enforce_sales_after_hunters(
            tasks_list, excluded_agents
        )
        tasks_list = normalize_iteration_task_ids(tasks_list, next_iter)

        created = 0
        for task_data in tasks_list:
            if "task_id" not in task_data:
                continue
            raw_agent = task_data.get("agent_name", "")
            if not isinstance(raw_agent, str) or raw_agent not in VALID_AGENTS:
                logger.error(
                    "❌ Итерация: недопустимый agent_name=%r для %s",
                    raw_agent, task_data.get("task_id"),
                )
                continue

            input_json = enrich_task_input(
                task_data,
                project=self.current_project,
                human_remarks=remarks,
                iteration=next_iter,
                previous_by_id=previous_by_id,
            )
            max_iter = task_data.get("max_iterations")
            if raw_agent == "developer":
                max_iter = max_iter or Config.DEVELOPER_MAX_ITERATIONS
            else:
                max_iter = max_iter or Config.MAX_TASK_ITERATIONS

            self.tasks_db.create_task({
                "task_id": task_data["task_id"],
                "project_id": project_id,
                "agent_name": raw_agent,
                "task_description": task_data.get("task_description") or "",
                "input_data": input_json,
                "status": "pending",
                "depends_on": json.dumps(
                    task_data.get("depends_on") or [], ensure_ascii=False
                ),
                "iteration_count": 0,
                "max_iterations": max_iter,
                "qa_approved": "pending",
                "created_at": datetime.now().isoformat(),
            })
            created += 1
            logger.info("  → Итерация %s: %s [%s]", next_iter, task_data["task_id"], raw_agent)

        if created == 0:
            raise RuntimeError("Не удалось создать ни одной задачи итерации")

        # Освобождаем другой in_progress
        try:
            active = self.projects_db.find_project_by_status("in_progress")
            if active and active.get("Id") and active.get("Id") != project_id:
                self.projects_db.update_project(active["Id"], {"status": "stopped"})
        except Exception as e:
            logger.warning("⚠️ Не удалось остановить другой активный проект: %s", e)

        plan_json = pm_task_graph.model_dump_json(indent=2)
        if len(plan_json) > 80000:
            plan_json = pm_task_graph.model_dump_json()
        if len(plan_json) > 80000:
            plan_json = json.dumps(
                {
                    "iteration": next_iter,
                    "tasks": [
                        {
                            "task_id": t.get("task_id"),
                            "agent_name": t.get("agent_name"),
                            "depends_on": t.get("depends_on") or [],
                        }
                        for t in tasks_list
                        if isinstance(t, dict)
                    ],
                    "excluded_agents": excluded_agents,
                    "reasoning": reasoning[:1500],
                    "truncated": True,
                },
                ensure_ascii=False,
            )

        metrics_json = json.dumps(metrics, ensure_ascii=False)
        update_fields: Dict[str, Any] = {
            "status": "in_progress",
            # "" для DateTime → SQLITE_ERROR в NocoDB; null очищает поле
            "completed_at": None,
            "tokens_used": self.current_project["tokens_used"],
            "metrics": metrics_json,
            "plan": plan_json,
            "excluded_agents": json.dumps(excluded_agents, ensure_ascii=False),
            "reasoning": f"Итерация {next_iter}: {reasoning}"[:2000],
            # Опциональная колонка NocoDB (если нет — resilient дропнет)
            "iteration": next_iter,
        }

        ok = self.projects_db.update_project_resilient(project_id, update_fields)
        if not ok:
            raise RuntimeError(
                f"Не удалось обновить проект #{project_id} после создания задач итерации "
                f"(NocoDB/SQLite). Задачи созданы ({created}), но статус/metrics не записаны."
            )

        self.current_project.update({
            "status": "in_progress",
            "completed_at": "",
            "metrics": metrics_json,
            "iteration": next_iter,
            "reasoning": update_fields["reasoning"],
            "tokens_used": self.current_project["tokens_used"],
        })

        log_to_agent_logs(
            project_id=project_id,
            agent_name="PM",
            status="completed",
            task_description=(
                f"Итерация {next_iter}: {created} задач по замечаниям человека. {reasoning}"
            ),
            full_response=json.dumps(
                {
                    "iteration": next_iter,
                    "human_remarks": remarks[:2000],
                    "tasks_created": created,
                    "graph": pm_task_graph.model_dump(),
                },
                ensure_ascii=False,
            ),
            tokens_used=pm_tokens,
        )

        return {
            "iteration": next_iter,
            "tasks_created": created,
            "project_id": project_id,
            "reasoning": reasoning,
            "pm_comment": reasoning or f"Создана итерация {next_iter}",
        }

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

            self.add_tokens(pm_tokens)

            final_report = pm_final_report.final_report
            # Сохраняем номер итерации и историю — PM metrics их не знает
            merged_metrics = parse_metrics(pm_final_report.metrics)
            prev_metrics = parse_metrics(self.current_project.get("metrics"))
            if "iteration_history" in prev_metrics:
                merged_metrics["iteration_history"] = prev_metrics["iteration_history"]
            if "last_human_remarks" in prev_metrics:
                merged_metrics["last_human_remarks"] = prev_metrics["last_human_remarks"]
            merged_metrics["iteration"] = get_project_iteration(self.current_project)
            metrics_json = json.dumps(merged_metrics, ensure_ascii=False)
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

            self.add_tokens(pm_tokens)

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
        Каждая dev_* уже проходит QA Gate в TaskExecutor перед статусом completed.
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

    def _execute_ready_wave(
        self,
        ready_tasks: List[Dict[str, Any]],
        pm_prompt: str,
        all_tasks: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Запускает волну независимых ready-задач (последовательно при limit=1)."""
        max_parallel = max(1, int(Config.MAX_PARALLEL_TASKS or 1))
        wave = select_parallel_wave(ready_tasks, max_parallel)
        if not wave:
            return

        ids = [t.get("task_id") for t in wave]
        if len(wave) == 1:
            logger.info("▶ Последовательное выполнение: %s", ids[0])
            self.execute_task(wave[0], pm_prompt, all_tasks=all_tasks)
            return

        logger.info(
            "⚡ Параллельная волна (%s/%s): %s",
            len(wave), max_parallel, ids,
        )
        # Снимок графа для обогащения input_data — siblings не видят чужой mid-flight output
        snapshot = list(all_tasks or [])

        with ThreadPoolExecutor(
            max_workers=len(wave), thread_name_prefix="agency-task"
        ) as pool:
            futures = {
                pool.submit(self.execute_task, task, pm_prompt, snapshot): task
                for task in wave
            }
            for fut in as_completed(futures):
                task = futures[fut]
                tid = task.get("task_id")
                try:
                    ok = fut.result()
                    logger.info(
                        "  ← %s: %s", tid, "ok" if ok else "fail/retry"
                    )
                except Exception:
                    logger.exception("❌ Параллельная задача %s упала", tid)

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
            for ctx_key in (
                "leads_context",
                "sales_context",
                "analyst_context",
                "client_hunter_context",
                "client_hunter_handoff_to_sales",
                "leads_handoff_to_sales",
            ):
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

    def _handle_client_hunter(self, task, task_db_id, task_name, agent_response, pm_prompt) -> bool:
        return self.agent_handlers.handle_client_hunter(
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
