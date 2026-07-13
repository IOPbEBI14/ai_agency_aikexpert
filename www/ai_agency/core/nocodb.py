import requests
import logging
import json
from typing import Dict, Any, Optional, List
from datetime import datetime
from urllib.parse import quote
from .config import Config

logger = logging.getLogger("NocoDBClient")


class NocoDBClient:
    """Клиент для работы с таблицей agent_logs"""

    def __init__(self):
        self.records_url = Config.get_nocodb_records_url()
        self.headers = {
            "xc-token": Config.NOCODB_API_TOKEN,
            "Content-Type": "application/json"
        }
        logger.info(f"🔌 NocoDBClient инициализирован: {self.records_url}")

    def create_record(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Создаёт запись в agent_logs"""
        try:
            payload = [{"fields": data}]
            response = requests.post(self.records_url, json=payload, headers=self.headers, timeout=120)
            response.raise_for_status()
            result = response.json()
            logger.info(f"✅ Сохранена запись: {data.get('agent_name')}")
            return result
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Ошибка сохранения: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text[:300]}")
            return None

    def get_recent_records(self, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Получает последние записи из agent_logs.
        Распаковывает fields на верхний уровень + сохраняет Id.
        """
        try:
            url = f"{self.records_url}?limit={limit}"
            response = requests.get(url, headers=self.headers, timeout=120)
            response.raise_for_status()
            data = response.json()
            raw_records = data.get("records", [])

            records = []
            for r in raw_records:
                fields = r.get("fields", {})
                fields["Id"] = r.get("Id")
                records.append(fields)

            logger.debug(f"📥 Получено {len(records)} записей из NocoDB")
            return records
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Ошибка чтения: {e}")
            return []


class ProjectsClient:
    """Клиент для работы с таблицей projects"""

    def __init__(self):
        self.projects_url = Config.get_nocodb_projects_url()
        self.headers = {
            "xc-token": Config.NOCODB_API_TOKEN,
            "Content-Type": "application/json"
        }
        logger.info(f"🔌 ProjectsClient инициализирован: {self.projects_url}")

    def _unpack_record(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        Распаковывает запись NocoDB API v3.
        ВАЖНО: в API v3 ID записи находится в поле 'id' (с маленькой буквы),
        а не 'Id' как в API v2.
        """
        if not raw:
            return {}
        
        fields = raw.get("fields", raw)
        
        # В API v3 ID записи может быть в разных полях
        record_id = raw.get("id") or raw.get("Id") or raw.get("row_id")
        fields["Id"] = record_id  # Сохраняем на верхнем уровне для совместимости
        
        # Логируем для отладки (убрать после исправления)
        if record_id is None:
            logger.warning(f"⚠️ Не удалось извлечь ID из записи. Ключи: {list(raw.keys())}")
        
        return fields

    def _build_where_url(self, field: str, value: str, limit: int = 1, sort_field: str = None) -> str:
        """Строит URL с фильтрацией для NocoDB API v3.
        Синтаксис: ?where=(field,eq,value)&limit=N&sort=[{"field":"...","direction":"..."}]
        """
        where_value = f"({field},eq,{value})"
        url = f"{self.projects_url}?where={quote(where_value)}&limit={limit}"

        if sort_field:
            sort_json = json.dumps([{"field": sort_field, "direction": "desc"}])
            url += f"&sort={quote(sort_json)}"

        return url  # Bug fix: was inside if-block → returned None when sort_field=None

    def list_projects(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Возвращает список проектов (новые сверху)."""
        try:
            sort_json = json.dumps([{"field": "UpdatedAt", "direction": "desc"}])
            url = f"{self.projects_url}?limit={limit}&sort={quote(sort_json)}"
            response = requests.get(url, headers=self.headers, timeout=120)
            if response.status_code != 200:
                # Fallback без sort (поле UpdatedAt может отсутствовать)
                url = f"{self.projects_url}?limit={limit}"
                response = requests.get(url, headers=self.headers, timeout=120)
            if response.status_code != 200:
                logger.error(f"❌ list_projects: {response.status_code} — {response.text[:200]}")
                return []
            data = response.json()
            records = [self._unpack_record(r) for r in data.get("records", [])]
            logger.debug(f"📥 list_projects: {len(records)} проектов")
            return records
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Ошибка list_projects: {e}")
            return []

    def find_project_by_status(self, status: str) -> Optional[Dict[str, Any]]:
        """Ищет проект по статусу, сортируя по дате обновления (новейший первый)."""
        try:
            url = self._build_where_url(
                field="status",
                value=status,
                limit=1,
                sort_field="UpdatedAt",
            )
            logger.info(f"🔍 Поиск проекта со статусом '{status}': {url}")
            
            response = requests.get(url, headers=self.headers, timeout=120)
            
            if response.status_code != 200:
                logger.error(f"? NocoDB вернул {response.status_code}: {response.text[:200]}")
                return None
            
            data = response.json()
            records = data.get("records", [])

            if records:
                # Логируем первую запись для отладки
                logger.debug(f"?? Первая запись: {json.dumps(records[0], ensure_ascii=False)[:300]}")
                
                project = self._unpack_record(records[0])
                logger.info(f"?? Найден проект в статусе '{status}': {project.get('project_name')} (ID: {project.get('Id')})")
                return project
            else:
                logger.info(f"?? Проект в статусе '{status}' не найден")
                return None
        except requests.exceptions.RequestException as e:
            logger.error(f"? Ошибка поиска проекта: {e}")
            return None

    def find_project_by_name(self, project_name: str) -> Optional[Dict[str, Any]]:
        """Ищет проект по имени."""
        try:
            url = self._build_where_url("project_name", project_name, limit=1)
            logger.info(f"🔍 Поиск проекта по имени: {url}")
            
            response = requests.get(url, headers=self.headers, timeout=120)
            
            if response.status_code != 200:
                logger.error(f"❌ NocoDB вернул {response.status_code}: {response.text[:200]}")
                return None
            
            data = response.json()
            records = data.get("records", [])

            if records:
                project = self._unpack_record(records[0])
                logger.info(f"📂 Найден проект по имени: {project.get('project_name')} (ID: {project.get('Id')})")
                return project
            return None
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Ошибка поиска проекта: {e}")
            return None

    def create_project(self, project_name: str, client_name: str, goal: str, token_budget: int) -> Dict[str, Any]:
        """Создаёт новый проект."""
        try:
            new_project = {
                "project_name": project_name,
                "client_name": client_name,
                "goal": goal,
                "current_phase": "lead_gen",
                "status": "in_progress",
                "tokens_used": 0,
                "token_budget": token_budget,
                "completed_agents": json.dumps([], ensure_ascii=False),
                "last_agent": "",
                "created_at": datetime.now().isoformat(),
                "updateTime": datetime.now().isoformat()
            }

            payload = [{"fields": new_project}]
            logger.info(f"🆕 Создание проекта: {project_name}")
            logger.debug(f"   Payload: {payload}")
            
            response = requests.post(self.projects_url, json=payload, headers=self.headers, timeout=120)
            
            if response.status_code >= 400:
                logger.error(f"❌ Ошибка создания проекта: {response.status_code} — {response.text[:300]}")
                # Возвращаем дефолтное состояние
                return {
                    "Id": None,
                    "project_name": project_name,
                    "client_name": client_name,
                    "goal": goal,
                    "current_phase": "lead_gen",
                    "status": "in_progress",
                    "tokens_used": 0,
                    "token_budget": token_budget,
                    "completed_agents": "[]",
                    "last_agent": ""
                }
            
            result = response.json()

            # Возвращаем созданную запись
            if isinstance(result, list) and len(result) > 0:
                project = self._unpack_record(result[0])
            elif isinstance(result, dict) and "records" in result:
                records = result.get("records", [])
                project = self._unpack_record(records[0]) if records else {}
            else:
                project = self._unpack_record(result)

            logger.info(f"✅ Создан новый проект: {project_name} (ID: {project.get('Id')})")
            return project
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Ошибка создания проекта: {e}")
            return {
                "Id": None,
                "project_name": project_name,
                "client_name": client_name,
                "goal": goal,
                "current_phase": "lead_gen",
                "status": "in_progress",
                "tokens_used": 0,
                "token_budget": token_budget,
                "completed_agents": "[]",
                "plan": json.dumps([], ensure_ascii=False),  # ← ДОБАВЛЕНО
                "last_agent": ""
            }

    def get_or_create_project(self, project_name: str, client_name: str, goal: str, token_budget: int) -> Dict[str, Any]:
        """Получает существующий проект по имени или создаёт новый."""
        project = self.find_project_by_name(project_name)
        if project:
            return project
        return self.create_project(project_name, client_name, goal, token_budget)

    def update_project(self, project_id: str, data: Dict[str, Any]) -> bool:
        """Обновляет состояние проекта через NocoDB API v3."""
        try:
            if not project_id:
                logger.error("? Нельзя обновить проект без Id")
                return False

            data["updated_at"] = datetime.now().isoformat()

            # Сериализация completed_agents в JSON
            if "completed_agents" in data and isinstance(data["completed_agents"], list):
                data["completed_agents"] = json.dumps(data["completed_agents"], ensure_ascii=False)

            # ВАЖНО: в API v3 поле называется "id" (с маленькой буквы), а не "Id"
            payload = [{"id": project_id, "fields": data}]
            
            logger.debug(f"?? PATCH payload: {json.dumps(payload, ensure_ascii=False)[:200]}")
            
            response = requests.patch(self.projects_url, json=payload, headers=self.headers, timeout=120)
            
            if response.status_code >= 400:
                logger.error(f"? Ошибка обновления проекта: {response.status_code} — {response.text[:300]}")
                return False
            
            logger.info(f"? Проект обновлён: {project_id}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"? Ошибка обновления проекта: {e}")
            return False

    def get_recent_records_by_project(self, project_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        """
        Получает последние записи для конкретного проекта.
        Фильтрует по project_id.
        """
        try:
            where_value = f"(project_id,eq,{project_id})"
            # Bug fix: was self.records_url (AttributeError) — ProjectsClient has projects_url
            url = f"{self.projects_url}?where={quote(where_value)}&limit={limit}"
            
            response = requests.get(url, headers=self.headers, timeout=120)
            response.raise_for_status()
            data = response.json()
            raw_records = data.get("records", [])

            records = []
            for r in raw_records:
                fields = r.get("fields", {})
                fields["Id"] = r.get("Id")
                records.append(fields)

            logger.info(f" Получено {len(records)} записей для проекта {project_id}")
            return records
        except requests.exceptions.RequestException as e:
            logger.error(f" Ошибка чтения записей проекта {project_id}: {e}")
            return []
    def find_project_by_id(self, project_id: int) -> Optional[Dict[str, Any]]:
        """Ищет проект по ID."""
        try:
            url = f"{self.projects_url}/{project_id}"
            response = requests.get(url, headers=self.headers, timeout=120)
            
            if response.status_code != 200:
                logger.error(f" NocoDB вернул {response.status_code}: {response.text[:200]}")
                return None
            
            data = response.json()
            project = self._unpack_record(data)
            logger.info(f"📂 Найден проект по ID: {project.get('project_name')} (ID: {project.get('Id')})")
            return project
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Ошибка поиска проекта: {e}")
            return None            
            
class TasksClient:
    """Клиент для работы с таблицей tasks"""

    def __init__(self):
        self.tasks_url = Config.get_nocodb_tasks_url()
        self.headers = {
            "xc-token": Config.NOCODB_API_TOKEN,
            "Content-Type": "application/json"
        }
        logger.info(f"🔌 TasksClient инициализирован: {self.tasks_url}")

    def _unpack_task(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        Распаковывает запись задачи из NocoDB API v3.
        КРИТИЧНО: извлекает id записи (с маленькой буквы).
        """
        if not raw:
            return {}
        
        fields = raw.get("fields", raw)
        
        # В API v3 ID записи в поле "id" (с маленькой буквы)
        record_id = raw.get("id") or raw.get("Id") or raw.get("row_id")
        
        # Сохраняем ID на верхнем уровне для использования в update_task
        fields["Id"] = record_id
        
        if record_id is None:
            logger.warning(f"⚠️ Не удалось извлечь ID задачи. Ключи записи: {list(raw.keys())}")
            logger.debug(f"🔍 Полная запись: {json.dumps(raw, ensure_ascii=False)[:300]}")
        
        return fields

    def create_task(self, task_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Создаёт новую задачу"""
        try:
            # Защита от попадания лог-строк в поле agent_name при создании
            raw_name = task_data.get("agent_name", "")
            if not isinstance(raw_name, str) or raw_name not in self._VALID_AGENT_NAMES:
                logger.error(
                    f"❌ Попытка создать задачу с недопустимым agent_name: "
                    f"{repr(str(raw_name)[:120])}. Задача НЕ создана."
                )
                return None

            payload = [{"fields": task_data}]
            logger.debug(f"📝 POST payload: {json.dumps(payload, ensure_ascii=False)[:300]}")
            
            response = requests.post(self.tasks_url, json=payload, headers=self.headers, timeout=120)
            
            if response.status_code >= 400:
                logger.error(f"❌ Ошибка создания задачи: {response.status_code} — {response.text[:300]}")
                return None
            
            result = response.json()
            logger.info(f"✅ Создана задача: {task_data.get('task_id')}")
            
            # Возвращаем распакованную запись
            if isinstance(result, list) and len(result) > 0:
                return self._unpack_task(result[0])
            elif isinstance(result, dict) and "records" in result:
                records = result.get("records", [])
                return self._unpack_task(records[0]) if records else None
            else:
                return self._unpack_task(result)
                
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Ошибка создания задачи: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text[:300]}")
            return None

    def get_tasks_by_project(self, project_id: int) -> List[Dict[str, Any]]:
        """Получает все задачи проекта"""
        try:
            where_value = f"(project_id,eq,{project_id})"
            url = f"{self.tasks_url}?where={quote(where_value)}&limit=100"
            
            response = requests.get(url, headers=self.headers, timeout=120)
            
            if response.status_code != 200:
                logger.error(f"❌ Ошибка чтения задач: {response.status_code} — {response.text[:200]}")
                return []
            
            data = response.json()
            raw_records = data.get("records", [])
            
            # Отладка: логируем первую запись
            if raw_records:
                first_record = raw_records[0]
                logger.debug(f"🔍 Первая запись задачи: {json.dumps(first_record, ensure_ascii=False)[:300]}")
            
            tasks = []
            for r in raw_records:
                task = self._unpack_task(r)
                tasks.append(task)
            
            logger.info(f"📥 Получено {len(tasks)} задач для проекта {project_id}")
            
            # Отладка: проверяем, что у всех задач есть Id
            tasks_without_id = [t for t in tasks if not t.get("Id")]
            if tasks_without_id:
                logger.warning(f"⚠️ {len(tasks_without_id)} задач без Id! Это приведёт к ошибкам обновления.")
            
            return tasks
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Ошибка чтения задач: {e}")
            return []

    # Допустимые имена агентов — защита от попадания посторонних строк в agent_name
    _VALID_AGENT_NAMES = frozenset({
        "PM", "client_hunter", "lead_hunter", "sales", "analyst", "architect",
        "developer", "crm_customizer", "qa", "tech_writer",
    })

    def update_task(self, task_id: str, data: Dict[str, Any]) -> bool:
        """Обновляет задачу через NocoDB API v3"""
        try:
            if not task_id:
                logger.error("❌ Нельзя обновить задачу без Id")
                return False

            # Защита от попадания лог-строк в поле agent_name (Инцидент №4)
            if "agent_name" in data:
                raw_name = data["agent_name"]
                if not isinstance(raw_name, str) or raw_name not in self._VALID_AGENT_NAMES:
                    logger.error(
                        f"❌ Попытка записать недопустимое значение в agent_name: "
                        f"{repr(str(raw_name)[:120])}. Поле исключено из PATCH."
                    )
                    del data["agent_name"]

            data["updated_at"] = datetime.now().isoformat()

            # Сериализация JSON-полей
            for field in ["input_data", "output_data", "depends_on", "qa_feedback"]:
                if field in data and isinstance(data[field], (dict, list)):
                    data[field] = json.dumps(data[field], ensure_ascii=False)
            
            # ВАЖНО: в API v3 поле "id" с маленькой буквы, а не "Id"!
            payload = [{"id": task_id, "fields": data}]
            
            logger.info(f"📝 PATCH задача {task_id}: {data}")
            logger.debug(f"📝 PATCH payload: {json.dumps(payload, ensure_ascii=False)[:300]}")
            
            response = requests.patch(self.tasks_url, json=payload, headers=self.headers, timeout=120)
            
            if response.status_code >= 400:
                logger.error(f"❌ Ошибка обновления задачи {task_id}: {response.status_code} — {response.text[:300]}")
                return False
            
            logger.info(f"✅ Задача обновлена: {task_id}")
            return True
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Ошибка обновления задачи {task_id}: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text[:300]}")
            return False

    def get_task_by_id(self, task_id: str) -> Optional[Dict[str, Any]]:
        """Получает задачу по ID"""
        try:
            if not task_id:
                return None
            url = f"{self.tasks_url}/{task_id}"
            response = requests.get(url, headers=self.headers, timeout=120)
            response.raise_for_status()
            data = response.json()
            return self._unpack_task(data)
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ Ошибка чтения задачи {task_id}: {e}")
            return None

    def check_all_subtasks_completed(self, parent_task_id: str) -> bool:
        """Проверяет, завершены ли все подзадачи для родительской задачи"""
        try:
            # Ищем все подзадачи, которые зависят от parent_task_id
            where_value = f"(depends_on,like,%{parent_task_id}%)"
            url = f"{self.tasks_url}?where={quote(where_value)}&limit=100"
            
            response = requests.get(url, headers=self.headers, timeout=120)
            response.raise_for_status()
            data = response.json()
            raw_records = data.get("records", [])
            
            if not raw_records:
                # Нет подзадач — значит это не родительская задача
                return True
            
            # Проверяем статус всех подзадач
            for r in raw_records:
                fields = r.get("fields", {})
                status = fields.get("status")
                if status != "completed":
                    return False
            
            return True
        except Exception as e:
            logger.error(f"Ошибка проверки подзадач: {e}")
            return False
            