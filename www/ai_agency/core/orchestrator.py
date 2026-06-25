"""
Orchestrator — главный класс, управляющий выполнением ИИ-агентства.
Реализует паттерн Supervisor-Workers с Pydantic-валидацией ответов LLM.
"""
import json
import logging
import time
from datetime import datetime
from typing import Dict, Any, Optional, List

from pydantic import ValidationError

from .config import Config
from .nocodb import NocoDBClient, ProjectsClient, TasksClient
from .schemas import (
    PMDecision, PMTaskGraph, PMDecomposition, PMFinalReport,
    PMHumanReview, PMDeadlockResolution,
    AnalystResponse, ArchitectResponse, DeveloperResponse,
    QAResponse, TechWriterResponse,
    LeadHunterResponse, SalesResponse, CRMCustomizerResponse,
    get_model_schema, extract_json_from_text, QAIssue, call_and_parse_llm
)
from core.utils import validate_with_qa, log_to_agent_logs, update_last_agent_log, load_prompt, call_llm, build_agent_task

logger = logging.getLogger("Orchestrator")


class Orchestrator:
    """
    Оркестратор ИИ-агентства с Pydantic-валидацией.
    
    Атрибуты:
        current_project: Текущий проект из NocoDB
        tasks_db: Клиент для работы с задачами
        projects_db: Клиент для работы с проектами
        agent_logs_db: Клиент для логирования
        agency_running: Флаг работы оркестратора
        agent_prompts_cache: Кэш загруженных промптов
    """
    
    MAX_TOTAL_ITERATIONS = 50
    
    # Маппинг агентов к их Pydantic-моделям
    AGENT_MODELS = {
        "pm_decision": PMDecision,
        "pm_task_graph": PMTaskGraph,
        "pm_decomposition": PMDecomposition,
        "pm_final_report": PMFinalReport,
        "pm_human_review": PMHumanReview,
        "pm_deadlock": PMDeadlockResolution,
        "analyst": AnalystResponse,
        "architect": ArchitectResponse,
        "developer": DeveloperResponse,
        "qa": QAResponse,
        "tech_writer": TechWriterResponse,
        "lead_hunter": LeadHunterResponse,
        "sales": SalesResponse,
        "crm_customizer": CRMCustomizerResponse,
    }
    
    def __init__(self):
        """Инициализация клиентов NocoDB."""
        self.agent_logs_db = NocoDBClient()
        self.projects_db = ProjectsClient()
        self.tasks_db = TasksClient()
        
        self.current_project: Optional[Dict[str, Any]] = None
        self.agency_running: bool = False
        self.agent_prompts_cache: Dict[str, str] = {}
        
        logger.info("🏗️ Orchestrator инициализирован")
    
    # ==================== ИНИЦИАЛИЗАЦИЯ ====================
    
    def initialize(self, project_id: Optional[int] = None) -> bool:
        """
        Инициализирует проект: загружает из NocoDB или создаёт новый.
        """
        if project_id:
            self.current_project = self.projects_db.find_project_by_id(project_id)
            if not self.current_project:
                logger.error(f"❌ Проект {project_id} не найден")
                return False
        else:
            # 1. Ищем активный проект
            self.current_project = self.projects_db.find_project_by_status("in_progress")
            
            if not self.current_project:
                # 2. Ищем остановленный проект
                self.current_project = (
                    self.projects_db.find_project_by_status("stopped")
                    or self.projects_db.find_project_by_status("needs_human_review")
                )
                
                if self.current_project and self.current_project.get("Id"):
                    logger.info(f"▶ Возобновляем проект: {self.current_project.get('project_name')}")
                    self.current_project["status"] = "in_progress"
                    self.projects_db.update_project(self.current_project["Id"], {"status": "in_progress"})
                else:
                    # 3. Создаём новый
                    logger.info("▶ Создаём новый проект")
                    self.current_project = self.projects_db.create_project(
                        project_name=Config.DEFAULT_PROJECT_NAME,
                        client_name=Config.DEFAULT_CLIENT_NAME,
                        goal=Config.DEFAULT_GOAL,
                        token_budget=Config.TOKEN_BUDGET
                    )
        
        if not self.current_project or not self.current_project.get("Id"):
            logger.error("❌ Не удалось получить/создать проект")
            return False
        
        # Обеспечиваем наличие обязательных полей
        if not self.current_project.get("project_name"):
            self.current_project["project_name"] = Config.DEFAULT_PROJECT_NAME
        if not self.current_project.get("client_name"):
            self.current_project["client_name"] = Config.DEFAULT_CLIENT_NAME
        if not self.current_project.get("status"):
            self.current_project["status"] = "in_progress"
        
        logger.info(f"✅ Проект инициализирован: {self.current_project.get('project_name')} (ID: {self.current_project.get('Id')})")
        return True
    
    # ==================== ВЫЗОВ LLM С ВАЛИДАЦИЕЙ ====================
    
    def call_agent_with_validation(
        self,
        agent_name: str,
        system_prompt: str,
        user_task: str,
        model_key: str,
        max_retries: int = 2
    ) -> tuple:
        """
        Вызывает агента и валидирует ответ через Pydantic.
        
        Args:
            agent_name: Имя агента
            system_prompt: Системный промпт (уже с JSON Schema)
            user_task: Задача для агента
            model_key: Ключ модели в AGENT_MODELS
            max_retries: Максимальное количество попыток
        
        Returns:
            (validated_model, tokens_used)
        
        Raises:
            ValueError: Если LLM не смог вернуть валидный JSON
        """
        #from main import call_llm
        
        model_class = self.AGENT_MODELS.get(model_key)
        if not model_class:
            raise ValueError(f"Неизвестная модель: {model_key}")
        
        current_prompt = user_task
        
        for attempt in range(max_retries + 1):
            try:
                # Вызываем LLM
                raw_response, tokens = call_llm(agent_name, system_prompt, current_prompt)
                
                logger.info(f"📏 Получен ответ от {agent_name}: {len(raw_response)} символов")
                
                # Извлекаем JSON
                json_str = extract_json_from_text(raw_response)
                data_dict = json.loads(json_str)
                
                # Валидируем через Pydantic
                validated_model = model_class(**data_dict)
                
                logger.info(f"✅ Успешная валидация для {model_class.__name__}")
                return validated_model, tokens
                
            except json.JSONDecodeError as e:
                logger.warning(f"⚠️ Попытка {attempt+1}: невалидный JSON: {e}")
                
                if attempt == max_retries:
                    raise ValueError(
                        f"LLM не смог вернуть валидный JSON для {model_class.__name__}: {e}"
                    )
                
                # Отправляем ошибку в LLM для исправления
                current_prompt = (
                    f"{user_task}\n\n"
                    f"⚠️ ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ БЫЛ НЕВАЛИДНЫМ.\n"
                    f"Ошибка парсинга JSON: {e}\n"
                    f"Ты должен вернуть ТОЛЬКО валидный JSON, строго соответствующий схеме.\n"
                    f"Убедись, что все скобки закрыты, все кавычки экранированы.\n"
                    f"Попробуй снова."
                )
                
            except ValidationError as e:
                logger.warning(f"⚠️ Попытка {attempt+1}: ошибка валидации Pydantic: {e}")
                
                if attempt == max_retries:
                    raise ValueError(
                        f"LLM вернул JSON, но он не соответствует схеме {model_class.__name__}: {e}"
                    )
                
                # Отправляем ошибку валидации в LLM
                current_prompt = (
                    f"{user_task}\n\n"
                    f"⚠️ ТВОЙ ПРЕДЫДУЩИЙ ОТВЕТ ПРОШЁЛ ПАРСИНГ JSON, НО НЕ ПРОШЁЛ ВАЛИДАЦИЮ.\n"
                    f"Ошибки валидации:\n{e}\n"
                    f"Ты должен исправить эти ошибки и вернуть ТОЛЬКО валидный JSON.\n"
                    f"Попробуй снова."
                )
            
            except Exception as e:
                logger.error(f"❌ Неожиданная ошибка при парсинге: {e}")
                
                if attempt == max_retries:
                    raise ValueError(f"Не удалось получить валидный ответ от {agent_name}: {e}")
        
        raise ValueError(f"Не удалось получить валидный ответ от {agent_name} после {max_retries+1} попыток")
    
    def build_prompt_with_schema(self, base_prompt: str, model_key: str) -> str:
        """
        Добавляет JSON Schema к базовому промпту.
        """
        model_class = self.AGENT_MODELS.get(model_key)
        if not model_class:
            return base_prompt
        
        schema = get_model_schema(model_class)
        
        schema_section = f"""

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
        
        return base_prompt + schema_section
    
    # ==================== ВСПОМОГАТЕЛЬНЫЕ МЕТОДЫ ====================
    
    def check_and_complete_parent_tasks(self, tasks: List[Dict[str, Any]]):
        """
        Проверяет родительские задачи: если все подзадачи завершены,
        помечает родителя как completed.
        """
        parent_candidates = {}
        
        for task in tasks:
            task_id = task.get("task_id", "")
            status = task.get("status", "")
            
            if task_id.startswith("dev_"):
                parent_id = "task_003"
                if parent_id not in parent_candidates:
                    parent_candidates[parent_id] = []
                parent_candidates[parent_id].append({
                    "task_id": task_id,
                    "status": status
                })
        
        for parent_id, subtasks in parent_candidates.items():
            all_completed = all(st["status"] == "completed" for st in subtasks)
            
            if all_completed:
                parent_task = next((t for t in tasks if t.get("task_id") == parent_id), None)
                if parent_task and parent_task.get("status") != "completed":
                    parent_db_id = parent_task.get("Id")
                    logger.info(f"✅ Все подзадачи {parent_id} завершены. Помечаем родителя как completed.")
                    self.tasks_db.update_task(parent_db_id, {
                        "status": "completed",
                        "qa_approved": "true",
                        "qa_feedback": f"Все {len(subtasks)} подзадач завершены успешно"
                    })
    
    def _expand_completed_with_parents(self, tasks: List[Dict[str, Any]], completed_ids: List[str]) -> List[str]:
        """
        Расширяет список completed_ids родителем, если все его подзадачи выполнены.
        """
        expanded = set(completed_ids)
        
        parent_subtasks = {}
        for task in tasks:
            task_id = task.get("task_id", "")
            status = task.get("status", "")
            
            if task_id.startswith("dev_"):
                parent_id = "task_003"
                if parent_id not in parent_subtasks:
                    parent_subtasks[parent_id] = []
                parent_subtasks[parent_id].append(status)
        
        for parent_id, statuses in parent_subtasks.items():
            if statuses and all(s == "completed" for s in statuses):
                expanded.add(parent_id)
        
        return list(expanded)
    
    def _find_ready_tasks(self, pending_tasks: List[Dict[str, Any]], completed_ids: List[str]) -> List[Dict[str, Any]]:
        """
        Находит задачи, у которых все зависимости выполнены.
        """
        ready = []
        for task in pending_tasks:
            depends_on = task.get("depends_on", "[]")
            try:
                depends_on = json.loads(depends_on) if isinstance(depends_on, str) else depends_on
            except:
                depends_on = []
            
            if all(dep_id in completed_ids for dep_id in depends_on):
                ready.append(task)
        return ready
        
    # ==================== ГЛАВНЫЙ ЦИКЛ ====================
    
    def run(self):
        """
        Главный цикл оркестратора.
        Выполняет задачи до их завершения или исчерпания бюджета.
        """
        #from main import load_prompt, log_to_agent_logs
        
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
            
            # Проверка бюджета
            remaining = token_budget - tokens_used
            if remaining < (token_budget * 0.2):
                logger.warning(f"⚠️ Бюджет на исходе: {tokens_used}/{token_budget}")
                self.current_project["status"] = "needs_human_review"
                self.projects_db.update_project(project_id, {
                    "status": "needs_human_review",
                    "tokens_used": tokens_used
                })
                log_to_agent_logs(
                    project_id=project_id,
                    agent_name="PM",
                    status="needs_review",
                    task_description=f"Автоматическая остановка: использовано {tokens_used} из {token_budget} токенов.",
                    full_response=json.dumps({
                        "reason": "budget_exhausted",
                        "tokens_used": tokens_used,
                        "token_budget": token_budget
                    }, ensure_ascii=False),
                    tokens_used=0
                )
                break
            
            # Инициализация Task Graph если нужно
            if not self._create_initial_task_graph():
                break
            
            # Получаем задачи и проверяем родительские
            tasks = self.tasks_db.get_tasks_by_project(project_id)
            self.check_and_complete_parent_tasks(tasks)
            tasks = self.tasks_db.get_tasks_by_project(project_id)
            
            # Анализ состояния
            completed = [t for t in tasks if t.get("status") == "completed"]
            failed = [t for t in tasks if t.get("status") == "failed"]
            pending = [t for t in tasks if t.get("status") == "pending"]
            in_progress = [t for t in tasks if t.get("status") == "in_progress"]
            
            logger.info(
                f"📊 Статус: completed={len(completed)}, pending={len(pending)}, "
                f"in_progress={len(in_progress)}, failed={len(failed)}"
            )
            
            # Проверка завершения всех задач → финализация
            if not pending and not in_progress and completed:
                logger.info(f"🏁 Все задачи завершены: {len(completed)} выполнено, {len(failed)} провалено")
                self.finalize(pm_prompt, tasks)
                break
            
            if not pending and not in_progress:
                logger.warning("⚠️ Нет задач для выполнения")
                break
            
            # Поиск готовых задач
            completed_ids = self._expand_completed_with_parents(tasks, [t.get("task_id") for t in completed])
            ready_tasks = self._find_ready_tasks(pending, completed_ids)
            
            logger.info(f"🎯 Готовых задач: {len(ready_tasks)}")
            for t in ready_tasks:
                logger.info(f"   - {t.get('task_id')} ({t.get('agent_name')})")
            
            if not ready_tasks and in_progress:
                logger.info(" Ждём завершения текущих задач...")
                time.sleep(5)
                continue
            
            if not ready_tasks:
                if pending:
                    logger.warning(f"⚠️ Тупик: {len(pending)} задач в pending")
                    if not self.resolve_deadlock(pm_prompt, tasks, pending):
                        logger.error(" PM не смог разрешить тупик")
                        break
                else:
                    logger.warning("️ Нет готовых задач — возможно, тупик")
                    break
                continue
            
            # Выполняем первую готовую задачу
            self.execute_task(ready_tasks[0], pm_prompt)
        
        self.agency_running = False
        logger.info(f"📊 ИТОГ: статус={self.current_project.get('status')}, токенов={self.current_project.get('tokens_used')}")
    
    def stop(self):
        """Останавливает оркестратор."""
        self.agency_running = False
        if self.current_project and self.current_project.get("Id"):
            self.current_project["status"] = "stopped"
            self.projects_db.update_project(self.current_project["Id"], {"status": "stopped"})
        logger.info("⏹ Оркестратор остановлен")
    
    # ==================== СОЗДАНИЕ TASK GRAPH ====================
    
    def _create_initial_task_graph(self) -> bool:
        """
        Создаёт начальный Task Graph через PM, если задач ещё нет.
        """
        #from main import load_prompt, call_llm, log_to_agent_logs
        
        project_id = self.current_project.get("Id")
        tasks = self.tasks_db.get_tasks_by_project(project_id)
        
        if tasks:
            return True
        
        logger.info("🧠 PM строит Task Graph...")
        
        pm_prompt = load_prompt("pm")
        task_graph_prompt = f"""
    Ты — Project Manager. Декомпозируй проект на задачи и построй граф зависимостей.

    ПРОЕКТ:
    Клиент: {self.current_project.get('client_name')}
    Цель: {self.current_project.get('goal')}

    ДОСТУПНЫЕ АГЕНТЫ:
    - analyst: анализ потребностей и ROI
    - architect: проектирование архитектуры (после analyst)
    - developer: разработка автоматизаций (после architect, будет декомпозирован на подзадачи)
    - crm_customizer: настройка CRM (после developer)
    - qa: тестирование и валидация (после crm_customizer)
    - tech_writer: документация (после qa)

    ВАЖНО:
    - analyst и architect — это ОДНА задача каждый
    - developer — это ОДНА задача (после её выполнения PM декомпозирует на подзадачи)
    - crm_customizer, qa, tech_writer — по одной задаче

    ⚠️ КРИТИЧНО: Используй поле "task_id" (НЕ "id"!) для идентификации задач!

    ПОСТРОЙ Task Graph в формате JSON:
    {{
        "tasks": [
            {{
                "task_id": "task_001",
                "agent_name": "analyst",
                "task_description": "Провести анализ...",
                "depends_on": [],
                "input_data": {{}},
                "max_iterations": 3
            }},
            {{
                "task_id": "task_002",
                "agent_name": "architect",
                "task_description": "Спроектировать архитектуру...",
                "depends_on": ["task_001"],
                "input_data": {{"roi_data": "результат analyst"}},
                "max_iterations": 3
            }}
        ]
    }}

    Верни ТОЛЬКО валидный JSON.
    """
        try:
            pm_response, pm_tokens = call_llm("PM", pm_prompt, task_graph_prompt)
            
            self.current_project["tokens_used"] = (self.current_project.get("tokens_used", 0) or 0) + pm_tokens
            self.projects_db.update_project(project_id, {"tokens_used": self.current_project["tokens_used"]})
            
            task_graph = json.loads(pm_response)
            tasks_list = task_graph.get("tasks", [])
            
            if not tasks_list:
                logger.error("❌ PM не вернул задачи")
                return False
            
            for task_data in tasks_list:
                task_data["project_id"] = project_id
                task_data["status"] = "pending"
                task_data["iteration_count"] = 0
                task_data["qa_approved"] = "pending"
                task_data["created_at"] = datetime.now().isoformat()
                self.tasks_db.create_task(task_data)

            # Сохраняем Task Graph в поле plan проекта
            if self.current_project.get("Id"):
                self.projects_db.update_project(
                    self.current_project["Id"],
                    {"plan": json.dumps(task_graph, ensure_ascii=False)}
                )            
            logger.info(f"✅ Task Graph создан: {len(tasks_list)} задач")
            
            log_to_agent_logs(
                project_id=project_id,
                agent_name="PM",
                status="completed",
                task_description=f"Построен Task Graph из {len(tasks_list)} задач",
                full_response=json.dumps(task_graph, ensure_ascii=False),
                tokens_used=0
            )
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка построения Task Graph: {e}", exc_info=True)
            return False
    # ==================== ВЫПОЛНЕНИЕ ЗАДАЧИ ====================
    
    def execute_task(self, task: Dict[str, Any], pm_prompt: str) -> bool:
        """
        Выполняет одну задачу с Pydantic-валидацией.
        """
        #from main import load_prompt, build_agent_task, log_to_agent_logs, update_last_agent_log
        
        project_id = self.current_project.get("Id")
        task_db_id = task.get("Id")
        task_name = task.get("task_id")
        agent_name = task.get("agent_name")
        task_description = task.get("task_description")
        input_data = task.get("input_data", "{}")
        iteration_count = task.get("iteration_count", 0) or 0
        max_iter = task.get("max_iterations", Config.MAX_TASK_ITERATIONS) or Config.MAX_TASK_ITERATIONS
        qa_feedback = task.get("qa_feedback", "")
        
        try:
            input_data = json.loads(input_data) if isinstance(input_data, str) else input_data
        except:
            input_data = {}
        
        if iteration_count >= max_iter:
            logger.warning(f"⚠️ Задача {task_name} превысила лимит итераций ({max_iter})")
            self.tasks_db.update_task(task_db_id, {
                "status": "failed",
                "qa_feedback": "Превышен лимит итераций"
            })
            return False
        
        self.tasks_db.update_task(task_db_id, {
            "status": "in_progress",
            "iteration_count": iteration_count + 1
        })
        
        logger.info(f"▶ Выполнение задачи {task_name} ({agent_name}), итерация {iteration_count + 1}/{max_iter}")
        
        # Загружаем промпт агента (с кэшем)
        if agent_name not in self.agent_prompts_cache:
            self.agent_prompts_cache[agent_name] = load_prompt(agent_name)
        agent_prompt = self.agent_prompts_cache[agent_name]
        
        # Определяем модель для валидации
        model_key = agent_name  # Например, "analyst", "architect", "developer"
        
        # Добавляем JSON Schema к промпту
        schema_prompt = self.build_prompt_with_schema(agent_prompt, model_key)
        
        # Формируем задачу для агента
        agent_task = build_agent_task(task_description, input_data, qa_feedback, iteration_count)
        
        try:
            # Вызываем агента с валидацией
            validated_response, agent_tokens = self.call_agent_with_validation(
                agent_name, schema_prompt, agent_task, model_key
            )
            
            # Обновляем бюджет
            self.current_project["tokens_used"] = (self.current_project.get("tokens_used", 0) or 0) + agent_tokens
            self.projects_db.update_project(project_id, {"tokens_used": self.current_project["tokens_used"]})
            
            # Получаем JSON для логирования
            response_json = validated_response.model_dump_json(indent=2)
            
            # Логируем
            log_to_agent_logs(
                project_id=project_id,
                agent_name=agent_name,
                status="review",
                task_description=f"[{task_name}] Итерация {iteration_count + 1}: {task_description[:150]}",
                full_response=response_json,
                tokens_used=agent_tokens
            )
            
            self.tasks_db.update_task(task_db_id, {
                "output_data": response_json,
                "tokens_used": (task.get("tokens_used", 0) or 0) + agent_tokens
            })
            
            # Особая обработка для architect → декомпозиция
            if agent_name == "architect":
                return self._handle_architect(task, task_db_id, task_name, validated_response, pm_prompt)
            
            # QA Gate
            if agent_name != "qa":
                return self.run_qa_gate(task, task_db_id, task_name, agent_name, response_json, task_description, iteration_count, max_iter)
            else:
                # Сам QA — просто завершаем
                self.tasks_db.update_task(task_db_id, {
                    "status": "completed",
                    "qa_approved": "true"
                })
                update_last_agent_log(project_id, agent_name, "completed")
                logger.info(f"✅ Задача QA {task_name} выполнена")
                return True
        
        except Exception as e:
            logger.error(f"❌ Ошибка выполнения задачи {task_name}: {e}", exc_info=True)
            self.tasks_db.update_task(task_db_id, {
                "status": "pending" if iteration_count + 1 < max_iter else "failed",
                "qa_feedback": f"Ошибка: {str(e)[:500]}"
            })
            return False
    
    def _handle_architect(self, task, task_db_id, task_name, agent_response, pm_prompt) -> bool:
        """Обрабатывает результат architect: QA → декомпозиция на подзадачи для developer."""
        #from main import call_llm, log_to_agent_logs, update_last_agent_log
        
        project_id = self.current_project.get("Id")
        
        # QA проверка
        logger.info(f"🔍 QA-проверка для {task_name}...")
        qa_result = self.run_qa_gate(task, task_db_id, task_name, "architect", 
                                      agent_response, task.get("task_description"), 0, 3, 
                                      update_status=False)
        
        logger.info(f"📋 QA результат: {qa_result}")  # ⭐ ДОБАВИТЬ
        
        if not qa_result:
            logger.error(f"❌ QA не прошел для {task_name}")  # ⭐ ДОБАВИТЬ
            return False
        
        # Декомпозиция на подзадачи
        logger.info(f"✅ Architect прошёл QA. Начинаю декомпозицию...")  # ⭐ ДОБАВИТЬ
        
        decompose_prompt = f"""
    Ты — Project Manager. Архитектор завершил проектирование. Разбей архитектуру на подзадачи для developer.

    АРХИТЕКТУРА ОТ ARCHITECT:
    {agent_response[:4000]}

    ЦЕЛЬ ПРОЕКТА:
    {self.current_project.get('goal')}

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
    - Верни ТОЛЬКО валидный JSON.
    """
        
        try:
            logger.info(f"🤖 Вызов PM для декомпозиции...")  # ⭐ ДОБАВИТЬ
            
            pm_response, pm_tokens = call_llm("PM", pm_prompt, decompose_prompt)
            self.current_project["tokens_used"] = (self.current_project.get("tokens_used", 0) or 0) + pm_tokens
            self.projects_db.update_project(project_id, {"tokens_used": self.current_project["tokens_used"]})
            
            pm_decision = json.loads(pm_response)
            subtasks = pm_decision.get("subtasks", [])
            
            logger.info(f"📦 PM вернул {len(subtasks)} подзадач")  # ⭐ ДОБАВИТЬ
            
            if not subtasks:
                logger.error(f"❌ PM не вернул подзадачи")  # ⭐ ДОБАВИТЬ
                self.tasks_db.update_task(task_db_id, {"status": "failed"})
                return False
            
            logger.info(f"✅ PM декомпозировал на {len(subtasks)} подзадач")
            
            for subtask in subtasks:
                subtask_data = {
                    "task_id": subtask.get("subtask_id"),
                    "project_id": project_id,
                    "agent_name": "developer",
                    "task_description": subtask.get("description"),
                    "input_data": json.dumps({
                        "context": subtask.get("context", ""),
                        "architecture_summary": agent_response[:2000]
                    }, ensure_ascii=False),
                    "status": "pending",
                    "depends_on": json.dumps(subtask.get("depends_on", []), ensure_ascii=False),
                    "iteration_count": 0,
                    "max_iterations": 3,
                    "qa_approved": "pending",
                    "created_at": datetime.now().isoformat()
                }
                self.tasks_db.create_task(subtask_data)
                logger.info(f"  → Создана подзадача: {subtask.get('subtask_id')}")  # ⭐ ДОБАВИТЬ
            
            # ⭐ ВАЖНО: Помечаем родительскую задачу как completed
            if task_db_id:
                logger.info(f"✅ Помечаю задачу {task_name} как completed")  # ⭐ ДОБАВИТЬ
                self.tasks_db.update_task(task_db_id, {
                    "status": "completed",
                    "qa_approved": "true",
                    "qa_feedback": f"Декомпозирована на {len(subtasks)} подзадач: {', '.join([s.get('subtask_id') for s in subtasks])}"
                })
                logger.info(f"✅ Родительская задача {task_name} переведена в статус completed")
            
            log_to_agent_logs(
                project_id=project_id,
                agent_name="PM",
                status="completed",
                task_description=f"Декомпозиция задачи {task_name} на {len(subtasks)} подзадач для developer",
                full_response=json.dumps(pm_decision, ensure_ascii=False),
                tokens_used=pm_tokens
            )
            
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка декомпозиции: {e}", exc_info=True)  # ⭐ ДОБАВИТЬ exc_info
            self.tasks_db.update_task(task_db_id, {"status": "failed"})
            return False
        
    # ==================== QA GATE ====================
    
    def _qa_response_to_result(self, qa_response: QAResponse) -> Dict[str, Any]:
        """
        Преобразует Pydantic-модель QAResponse в результат QA-проверки.
        
        Returns:
            dict с полями:
            - approved: bool
            - feedback: str
            - issues: list
            - tokens_used: int
        """
        # Определяем approved: нет проваленных тестов И нет критических/high issues
        has_critical_issues = any(
            issue.severity in ("critical", "high") 
            for issue in qa_response.issues
        )
        
        approved = (qa_response.tests_failed == 0) and not has_critical_issues
        
        # Формируем feedback
        feedback_parts = [qa_response.summary]
        
        if qa_response.issues:
            feedback_parts.append("\nНайденные проблемы:")
            for issue in qa_response.issues:
                feedback_parts.append(
                    f"- [{issue.severity.upper()}] {issue.description} "
                    f"(в {issue.location}) → {issue.recommendation}"
                )
        
        if qa_response.warnings:
            feedback_parts.append("\nПредупреждения:")
            for warning in qa_response.warnings:
                feedback_parts.append(f"- {warning}")
        
        feedback = "\n".join(feedback_parts)
        
        return {
            "approved": approved,
            "feedback": feedback,
            "issues": [
                {
                    "severity": issue.severity,
                    "type": issue.type,
                    "description": issue.description,
                    "location": issue.location,
                    "recommendation": issue.recommendation
                }
                for issue in qa_response.issues
            ],
            "summary": qa_response.summary,
            "tests_total": qa_response.tests_total,
            "tests_passed": qa_response.tests_passed,
            "tests_failed": qa_response.tests_failed,
            "tokens_used": 0  # Будет заполнено вызывающим кодом
        }


    def run_qa_gate(self, task, task_db_id, task_name, agent_name, 
                    agent_response, task_description, iteration_count, 
                    max_iter, update_status=True) -> bool:
        """
        Проверяет результат задачи через QA-агента с Pydantic-валидацией.
        """
        from core.schemas import QAResponse, call_and_parse_llm, get_model_schema
        from core.utils import load_prompt, log_to_agent_logs, update_last_agent_log
        
        project_id = self.current_project.get("Id")
        
        logger.info(f"🔍 QA-проверка для задачи {task_name}...")
        
        # Загружаем промпт QA
        qa_prompt = load_prompt("qa")
        
        # Добавляем JSON Schema к промпту
        schema = get_model_schema(QAResponse)
        schema_prompt = qa_prompt + f"""

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
        
        # Формируем задачу для QA
        agent_response_str = agent_response.model_dump_json(indent=2)
        qa_task = f"""
    ЗАДАЧА: {task_description}
    АГЕНТ: {agent_name}

    РЕЗУЛЬТАТ ДЛЯ ПРОВЕРКИ (длина: {len(agent_response_str)} символов):
    {agent_response[:6000]}

    ПРОВЕРЬ:
    1. Соответствует ли результат задаче?
    2. Нет ли ошибок или противоречий?
    3. Достаточно ли данных для следующих задач?
    4. Валиден ли JSON?
    """
        
        try:
            # Вызываем LLM с Pydantic-валидацией
            qa_response, qa_tokens = call_and_parse_llm(
                call_llm_func=lambda name, sys_prompt, user_task: self._call_llm_wrapper(name, sys_prompt, user_task),
                agent_name="qa",
                system_prompt=schema_prompt,
                user_task=qa_task,
                response_model=QAResponse,
                max_retries=2
            )
            
            logger.info(f"✅ QA вернул валидный ответ: {qa_response.summary}")
            logger.info(f"   Тестов: {qa_response.tests_total}, пройдено: {qa_response.tests_passed}, провалено: {qa_response.tests_failed}")
            
            # Обновляем бюджет
            self.current_project["tokens_used"] = (self.current_project.get("tokens_used", 0) or 0) + qa_tokens
            self.projects_db.update_project(project_id, {"tokens_used": self.current_project["tokens_used"]})
            
            # Преобразуем Pydantic-модель в результат QA
            qa_result = self._qa_response_to_result(qa_response)
            qa_result["tokens_used"] = qa_tokens
            
            qa_approved = qa_result["approved"]
            qa_feedback_text = qa_result["feedback"]
            
            # Логируем
            log_to_agent_logs(
                project_id=project_id,
                agent_name="qa",
                status="completed" if qa_approved else "needs_review",
                task_description=f"QA-проверка {task_name}: {'✅ ПРОШЁЛ' if qa_approved else '❌ НЕ ПРОШЁЛ'}",
                full_response=qa_response.model_dump_json(indent=2),
                tokens_used=qa_tokens
            )
            
            if not update_status:
                return qa_approved
            
            if qa_approved:
                self.tasks_db.update_task(task_db_id, {
                    "status": "completed",
                    "qa_approved": "true",
                    "qa_feedback": qa_feedback_text
                })
                update_last_agent_log(project_id, agent_name, "completed")
                logger.info(f"✅ Задача {task_name} ({agent_name}) выполнена и прошла QA")
                return True
            else:
                logger.warning(f"️ QA не прошёл для {task_name}: {qa_feedback_text[:200]}")
                self.tasks_db.update_task(task_db_id, {
                    "status": "pending",
                    "qa_approved": "false",
                    "qa_feedback": qa_feedback_text
                })
                update_last_agent_log(project_id, agent_name, "needs_review")
                
                if iteration_count + 1 >= max_iter:
                    logger.error(f"❌ Задача {task_name} провалена после {max_iter} итераций")
                    self.tasks_db.update_task(task_db_id, {"status": "failed"})
                    update_last_agent_log(project_id, agent_name, "failed")
                return False
                
        except Exception as e:
            logger.error(f" Ошибка QA: {e}", exc_info=True)
            
            if update_status:
                self.tasks_db.update_task(task_db_id, {
                    "status": "pending",
                    "qa_approved": "false",
                    "qa_feedback": f"Ошибка QA: {str(e)[:500]}"
                })
            
            return False


    def _call_llm_wrapper(self, agent_name: str, system_prompt: str, user_task: str) -> tuple:
        """
        Обёртка для call_llm, которая возвращает (content, tokens).
        """
        from core.utils import call_llm
        return call_llm(agent_name, system_prompt, user_task)


    def _qa_response_to_result(self, qa_response: QAResponse) -> Dict[str, Any]:
        """
        Преобразует Pydantic-модель QAResponse в результат QA-проверки.
        
        Returns:
            dict с полями:
            - approved: bool
            - feedback: str
            - issues: list
        """
        # Определяем approved: нет проваленных тестов И нет критических/high issues
        has_critical_issues = any(
            issue.severity in ("critical", "high") 
            for issue in qa_response.issues
        )
        
        approved = (qa_response.tests_failed == 0) and not has_critical_issues
        
        # Формируем feedback
        feedback_parts = [qa_response.summary]
        
        if qa_response.issues:
            feedback_parts.append("\nНайденные проблемы:")
            for issue in qa_response.issues:
                feedback_parts.append(
                    f"- [{issue.severity.upper()}] {issue.description} "
                    f"(в {issue.location}) → {issue.recommendation}"
                )
        
        if qa_response.warnings:
            feedback_parts.append("\nПредупреждения:")
            for warning in qa_response.warnings:
                feedback_parts.append(f"- {warning}")
        
        feedback = "\n".join(feedback_parts)
        
        return {
            "approved": approved,
            "feedback": feedback,
            "issues": [
                {
                    "severity": issue.severity,
                    "type": issue.type,
                    "description": issue.description,
                    "location": issue.location,
                    "recommendation": issue.recommendation
                }
                for issue in qa_response.issues
            ]
        }    
    # ==================== РАЗРЕШЕНИЕ ТУПИКОВ ====================
    
    def resolve_deadlock(self, pm_prompt: str, tasks: List[Dict[str, Any]], pending_tasks: List[Dict[str, Any]]) -> bool:
        """
        Вызывает PM для разрешения тупика с Pydantic-валидацией.
        """
        #from main import load_prompt
        
        project_id = self.current_project.get("Id")
        
        tasks_summary = [{
            "task_id": t.get("task_id"),
            "agent": t.get("agent_name"),
            "status": t.get("status"),
            "depends_on": t.get("depends_on", "[]")
        } for t in tasks]
        
        # Загружаем промпт PM
        pm_prompt_text = load_prompt("pm")
        
        # Добавляем JSON Schema для разрешения тупика
        schema_prompt = self.build_prompt_with_schema(pm_prompt_text, "pm_deadlock")
        
        user_task = f"""
ПРОЕКТ В ТУПИКЕ! Есть задачи в статусе pending, но ни одна не готова к выполнению.

ВСЕ ЗАДАЧИ ПРОЕКТА:
{json.dumps(tasks_summary, ensure_ascii=False, indent=2)}

ЗАДАЧИ В PENDING:
{json.dumps([{'task_id': t.get('task_id'), 'agent': t.get('agent_name'), 'depends_on': t.get('depends_on')} for t in pending_tasks], ensure_ascii=False, indent=2)}

ПРЕДЛОЖИ решение согласно схеме выше.
"""
        
        try:
            # Вызываем PM с валидацией
            pm_deadlock, pm_tokens = self.call_agent_with_validation(
                "PM", schema_prompt, user_task, "pm_deadlock"
            )
            
            # Обновляем бюджет
            self.current_project["tokens_used"] = (self.current_project.get("tokens_used", 0) or 0) + pm_tokens
            self.projects_db.update_project(project_id, {"tokens_used": self.current_project["tokens_used"]})
            
            logger.info(f" PM предложил решение: {pm_deadlock.solution}. Комментарий: {pm_deadlock.comment}")
            
            for action in pm_deadlock.actions:
                action_type = action.get("action")
                task_id = action.get("task_id")
                
                task = next((t for t in tasks if t.get("task_id") == task_id), None)
                if not task:
                    continue
                
                task_db_id = task.get("Id")
                
                if action_type == "update_task":
                    update_data = {}
                    if "new_status" in action:
                        update_data["status"] = action["new_status"]
                    if "new_depends_on" in action:
                        update_data["depends_on"] = json.dumps(action["new_depends_on"], ensure_ascii=False)
                    if update_data:
                        self.tasks_db.update_task(task_db_id, update_data)
                
                elif action_type == "skip_task":
                    self.tasks_db.update_task(task_db_id, {"status": "failed", "qa_feedback": "Пропущено по решению PM"})
            
            return pm_deadlock.solution != "stop_project"
            
        except Exception as e:
            logger.error(f" Ошибка разрешения тупика: {e}")
            return False
    
