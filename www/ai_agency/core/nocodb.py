import json
import logging
import random
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests

from .config import Config

logger = logging.getLogger("NocoDBClient")

# Сериализация HTTP внутри процесса: оркестратор + Flask/WS не бьют SQLite разом
_NOCODB_HTTP_LOCK = threading.RLock()


class NocoDBTransientError(Exception):
    """Временная ошибка NocoDB (сеть / 5xx / 429 / SQLITE_BUSY) — можно повторить."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        sqlite_busy: bool = False,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.sqlite_busy = sqlite_busy


def _is_sqlite_busy_message(msg: str) -> bool:
    """NocoDB latest на SQLite: ERR_DATABASE_OP_FAILED / SQLITE_BUSY."""
    text = (msg or "").lower()
    return (
        "sqlite_busy" in text
        or "database is locked" in text
        or "database locked" in text
    )


def _is_retryable_status(status_code: int) -> bool:
    return status_code == 429 or status_code >= 500


def _is_retryable_exc(exc: BaseException) -> bool:
    if isinstance(exc, NocoDBTransientError):
        return True
    if isinstance(exc, (
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
        requests.exceptions.ChunkedEncodingError,
    )):
        return True
    # Общий RequestException без response (обрыв) — retry; с 4xx — нет
    if isinstance(exc, requests.exceptions.HTTPError):
        resp = getattr(exc, "response", None)
        code = getattr(resp, "status_code", None) if resp is not None else None
        return bool(code and _is_retryable_status(code))
    if isinstance(exc, requests.exceptions.RequestException):
        resp = getattr(exc, "response", None)
        if resp is None:
            return True
        return _is_retryable_status(getattr(resp, "status_code", 0) or 0)
    return False


def _delay_for_attempt(
    *,
    attempt: int,
    sqlite_busy: bool,
    default_delays: Tuple[float, ...],
    busy_delays: Tuple[float, ...],
) -> float:
    """Пауза перед следующей попыткой; для SQLITE_BUSY — короче + jitter."""
    delays = busy_delays if sqlite_busy else default_delays
    if not delays:
        delays = (1.0,)
    base = float(delays[min(attempt - 1, len(delays) - 1)])
    if sqlite_busy:
        # Небольшой jitter, чтобы параллельные клиенты не били в одну фазу
        return max(10, base + random.uniform(1.0, min(5, base * 25.0)))
    return base


def nocodb_request(
    method: str,
    url: str,
    *,
    headers: Optional[Dict[str, str]] = None,
    json_body: Any = None,
    timeout: Optional[float] = None,
    raise_for_status: bool = False,
) -> requests.Response:
    """HTTP к NocoDB с таймаутом Config.NOCODB_TIMEOUT_SEC и ретраями.

    Обычные временные ошибки (сеть / timeout / 429 / 5xx):
      NOCODB_MAX_ATTEMPTS + NOCODB_RETRY_DELAYS_SEC (default 10, 30, 60).

    SQLITE_BUSY / database locked (NocoDB on SQLite, Direction AM):
      больше коротких попыток NOCODB_BUSY_* + jitter; HTTP под process-lock.
    """
    timeout = Config.NOCODB_TIMEOUT_SEC if timeout is None else timeout
    default_max = max(1, int(Config.NOCODB_MAX_ATTEMPTS))
    busy_max = max(default_max, int(Config.NOCODB_BUSY_MAX_ATTEMPTS))
    default_delays: Tuple[float, ...] = tuple(
        Config.NOCODB_RETRY_DELAYS_SEC
    ) or (10.0, 30.0, 60.0)
    busy_delays: Tuple[float, ...] = tuple(
        Config.NOCODB_BUSY_RETRY_DELAYS_SEC
    ) or (10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0)
    serialize = bool(Config.NOCODB_SERIALIZE_REQUESTS)

    # max_attempts может вырасти после первой SQLITE_BUSY
    max_attempts = default_max
    last_exc: Optional[BaseException] = None
    saw_busy = False

    for attempt in range(1, busy_max + 1):
        if attempt > max_attempts:
            break
        try:
            if serialize:
                with _NOCODB_HTTP_LOCK:
                    response = requests.request(
                        method.upper(),
                        url,
                        headers=headers,
                        json=json_body,
                        timeout=timeout,
                    )
            else:
                response = requests.request(
                    method.upper(),
                    url,
                    headers=headers,
                    json=json_body,
                    timeout=timeout,
                )
            body_preview = response.text[:400] if response.text else ""
            busy = _is_sqlite_busy_message(body_preview)
            if _is_retryable_status(response.status_code) or busy:
                if busy:
                    saw_busy = True
                    max_attempts = busy_max
                raise NocoDBTransientError(
                    f"HTTP {response.status_code}: {body_preview[:200]}",
                    status_code=response.status_code,
                    sqlite_busy=busy,
                )
            if raise_for_status:
                response.raise_for_status()
            return response
        except Exception as e:
            last_exc = e
            retryable = _is_retryable_exc(e)
            busy = bool(getattr(e, "sqlite_busy", False)) or _is_sqlite_busy_message(
                str(e)
            )
            if busy:
                saw_busy = True
                max_attempts = busy_max
            if (not retryable) or attempt >= max_attempts:
                if isinstance(e, NocoDBTransientError) and attempt >= max_attempts:
                    logger.error(
                        "❌ NocoDB %s %s: исчерпаны %s попыток%s — %s",
                        method.upper(),
                        url,
                        max_attempts,
                        " (SQLITE_BUSY)" if saw_busy else "",
                        e,
                    )
                raise
            delay = _delay_for_attempt(
                attempt=attempt,
                sqlite_busy=busy or saw_busy,
                default_delays=default_delays,
                busy_delays=busy_delays,
            )
            logger.warning(
                "⚠️ NocoDB %s %s попытка %s/%s не удалась (%s). Повтор через %.2f с…",
                method.upper(),
                url,
                attempt,
                max_attempts,
                e,
                delay,
            )
            time.sleep(delay)

    assert last_exc is not None
    raise last_exc


# Ловим и сетевые ошибки requests, и исчерпанные ретраи 5xx/429
_NOCODB_REQUEST_ERRORS = (requests.exceptions.RequestException, NocoDBTransientError)

# DateTime-поля projects: пустая строка "" ломает SQLite в NocoDB (SQLITE_ERROR «near …»)
_PROJECT_DATE_FIELDS = frozenset({
    "completed_at", "created_at", "updated_at", "updateTime",
})
_PROJECT_JSON_FIELDS = frozenset({
    "metrics", "plan", "excluded_agents", "completed_agents",
})


def _sanitize_project_patch_fields(data: Dict[str, Any]) -> Dict[str, Any]:
    """Готовит fields для PATCH projects: null вместо "" для дат, JSON → str."""
    out: Dict[str, Any] = {}
    for key, val in data.items():
        if isinstance(val, str) and "\x00" in val:
            val = val.replace("\x00", "")
        if key in _PROJECT_DATE_FIELDS and val == "":
            out[key] = None
            continue
        if key in _PROJECT_JSON_FIELDS and isinstance(val, (dict, list)):
            out[key] = json.dumps(val, ensure_ascii=False)
            continue
        out[key] = val
    return out


def build_records_list_url(
    records_url: str,
    *,
    where: Optional[str] = None,
    page: int = 1,
    page_size: int = 25,
    sort_field: Optional[str] = None,
    sort_direction: str = "desc",
    sort: Optional[List[Dict[str, str]]] = None,
    fields: Optional[List[str]] = None,
) -> str:
    """URL списка записей Data API v3 (openapi: page / pageSize / sort / where).

    ``limit`` из старого кода передавайте как ``page_size``.
    ``sort`` — JSON-массив ``[{field, direction}]``, URL-encoded (не nested qs).
    """
    parts: List[str] = []
    if where:
        parts.append(f"where={quote(where)}")
    parts.append(f"page={max(1, int(page))}")
    parts.append(f"pageSize={max(1, int(page_size))}")

    sort_list = sort
    if sort_list is None and sort_field:
        direction = sort_direction if sort_direction in ("asc", "desc") else "desc"
        sort_list = [{"field": sort_field, "direction": direction}]
    if sort_list:
        parts.append(f"sort={quote(json.dumps(sort_list, ensure_ascii=False))}")
    if fields:
        parts.append(f"fields={quote(','.join(fields))}")

    return f"{records_url}?{'&'.join(parts)}"


def unpack_data_record(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """DataRecordV3 → плоский dict; PK кладём в ``Id`` (совместимость приложения).

    OpenAPI: ``{ id, fields, id_fields? }``.
    """
    if not raw:
        return {}

    if isinstance(raw.get("fields"), dict) or "fields" in raw:
        fields = dict(raw.get("fields") or {})
    else:
        fields = {k: v for k, v in raw.items() if k not in ("id", "Id", "id_fields")}

    record_id = raw.get("id")
    if record_id is None:
        record_id = raw.get("Id") or raw.get("row_id")
    fields["Id"] = record_id

    if record_id is None:
        logger.warning(
            "⚠️ Не удалось извлечь id записи Data API v3. Ключи: %s",
            list(raw.keys()),
        )
    return fields


def extract_records_payload(payload: Any) -> List[Dict[str, Any]]:
    """Достаёт ``records[]`` из DataList/Insert/Update response."""
    if isinstance(payload, dict):
        recs = payload.get("records")
        if isinstance(recs, list):
            return recs
        # одиночная DataReadResponseV3
        if "id" in payload or "fields" in payload:
            return [payload]
        return []
    if isinstance(payload, list):
        return payload
    return []


def xc_headers(token: Optional[str] = None) -> Dict[str, str]:
    """Заголовок авторизации Data API v3 (``xc-token``)."""
    return {
        "xc-token": token if token is not None else Config.NOCODB_API_TOKEN,
        "Content-Type": "application/json",
    }


class NocoDBClient:
    """Клиент для работы с таблицей agent_logs (Data API v3)."""

    def __init__(self):
        self.records_url = Config.get_nocodb_records_url()
        self.headers = xc_headers()
        logger.info(f"🔌 NocoDBClient инициализирован: {self.records_url}")

    def create_record(self, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """POST /records — DataInsertRequestV3[] → DataInsertResponseV3."""
        try:
            payload = [{"fields": data}]
            response = nocodb_request(
                "POST", self.records_url, json_body=payload, headers=self.headers
            )
            response.raise_for_status()
            result = response.json()
            logger.info(f"✅ Сохранена запись: {data.get('agent_name')}")
            return result
        except _NOCODB_REQUEST_ERRORS as e:
            logger.error(f"❌ Ошибка сохранения: {e}")
            if hasattr(e, "response") and e.response is not None:
                logger.error(f"Response: {e.response.text[:300]}")
            return None

    def get_recent_records(self, limit: int = 10) -> List[Dict[str, Any]]:
        """GET /records?page=1&pageSize=N — последние записи agent_logs."""
        try:
            url = build_records_list_url(
                self.records_url,
                page=1,
                page_size=limit,
                sort_field="CreatedAt",
                sort_direction="desc",
            )
            response = nocodb_request("GET", url, headers=self.headers)
            if response.status_code != 200:
                # fallback: без sort (кастомные таблицы могут не иметь CreatedAt)
                url = build_records_list_url(
                    self.records_url, page=1, page_size=limit
                )
                response = nocodb_request("GET", url, headers=self.headers)
            response.raise_for_status()
            records = [
                unpack_data_record(r)
                for r in extract_records_payload(response.json())
            ]
            logger.debug(f"📥 Получено {len(records)} записей из NocoDB")
            return records
        except _NOCODB_REQUEST_ERRORS as e:
            logger.error(f"❌ Ошибка чтения: {e}")
            return []


class ProjectsClient:
    """Клиент для работы с таблицей projects (Data API v3)."""

    def __init__(self):
        self.projects_url = Config.get_nocodb_projects_url()
        self.headers = xc_headers()
        logger.info(f"🔌 ProjectsClient инициализирован: {self.projects_url}")

    def _unpack_record(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Распаковывает DataRecordV3 (id + fields)."""
        return unpack_data_record(raw)

    def _build_where_url(
        self,
        field: str,
        value: str,
        limit: int = 1,
        sort_field: str = None,
        page: int = 1,
    ) -> str:
        """URL с where/pageSize/sort (``limit`` → ``pageSize``, совместимость)."""
        return build_records_list_url(
            self.projects_url,
            where=f"({field},eq,{value})",
            page=page,
            page_size=limit,
            sort_field=sort_field,
            sort_direction="desc",
        )

    def list_projects(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Возвращает список проектов (новые сверху)."""
        try:
            url = build_records_list_url(
                self.projects_url,
                page=1,
                page_size=limit,
                sort_field="UpdatedAt",
                sort_direction="desc",
            )
            response = nocodb_request("GET", url, headers=self.headers)
            if response.status_code != 200:
                url = build_records_list_url(
                    self.projects_url, page=1, page_size=limit
                )
                response = nocodb_request("GET", url, headers=self.headers)
            if response.status_code != 200:
                logger.error(
                    f"❌ list_projects: {response.status_code} — {response.text[:200]}"
                )
                return []
            records = [
                self._unpack_record(r)
                for r in extract_records_payload(response.json())
            ]
            logger.debug(f"📥 list_projects: {len(records)} проектов")
            return records
        except _NOCODB_REQUEST_ERRORS as e:
            logger.error(f"❌ Ошибка list_projects: {e}")
            return []

    def find_project_by_status(self, status: str) -> Optional[Dict[str, Any]]:
        """Ищет проект по статусу, сортируя по UpdatedAt (новейший первый)."""
        try:
            url = self._build_where_url(
                field="status",
                value=status,
                limit=1,
                sort_field="UpdatedAt",
            )
            logger.debug("🔍 Поиск проекта status=%s url=%s", status, url)

            response = nocodb_request("GET", url, headers=self.headers)
            if response.status_code != 200:
                # fallback без sort — меньше нагрузка на SQLite / нет поля UpdatedAt
                url = self._build_where_url("status", status, limit=1)
                response = nocodb_request("GET", url, headers=self.headers)

            if response.status_code != 200:
                logger.error(
                    "? NocoDB вернул %s: %s",
                    response.status_code,
                    response.text[:200],
                )
                return None

            records = extract_records_payload(response.json())
            if records:
                project = self._unpack_record(records[0])
                logger.debug(
                    "?? Найден проект status=%s name=%s id=%s",
                    status,
                    project.get("project_name"),
                    project.get("Id"),
                )
                return project
            logger.debug("?? Проект status=%s не найден", status)
            return None
        except _NOCODB_REQUEST_ERRORS as e:
            logger.error(f"? Ошибка поиска проекта: {e}")
            return None

    def find_project_by_name(self, project_name: str) -> Optional[Dict[str, Any]]:
        """Ищет проект по имени."""
        try:
            url = self._build_where_url("project_name", project_name, limit=1)
            logger.info(f"🔍 Поиск проекта по имени: {url}")

            response = nocodb_request("GET", url, headers=self.headers)

            if response.status_code != 200:
                logger.error(
                    f"❌ NocoDB вернул {response.status_code}: {response.text[:200]}"
                )
                return None

            records = extract_records_payload(response.json())
            if records:
                project = self._unpack_record(records[0])
                logger.info(
                    f"📂 Найден проект по имени: {project.get('project_name')} "
                    f"(ID: {project.get('Id')})"
                )
                return project
            return None
        except _NOCODB_REQUEST_ERRORS as e:
            logger.error(f"❌ Ошибка поиска проекта: {e}")
            return None

    def create_project(
        self,
        project_name: str,
        client_name: str,
        goal: str,
        token_budget: int,
        current_phase: str = "lead_gen",
    ) -> Dict[str, Any]:
        """Создаёт новый проект."""
        try:
            new_project = {
                "project_name": project_name,
                "client_name": client_name,
                "goal": goal,
                "current_phase": current_phase or "lead_gen",
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
            
            response = nocodb_request("POST", self.projects_url, json_body=payload, headers=self.headers)
            
            if response.status_code >= 400:
                logger.error(f"❌ Ошибка создания проекта: {response.status_code} — {response.text[:300]}")
                # Возвращаем дефолтное состояние
                return {
                    "Id": None,
                    "project_name": project_name,
                    "client_name": client_name,
                    "goal": goal,
                    "current_phase": current_phase or "lead_gen",
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
        except _NOCODB_REQUEST_ERRORS as e:
            logger.error(f"❌ Ошибка создания проекта: {e}")
            return {
                "Id": None,
                "project_name": project_name,
                "client_name": client_name,
                "goal": goal,
                "current_phase": current_phase or "lead_gen",
                "status": "in_progress",
                "tokens_used": 0,
                "token_budget": token_budget,
                "completed_agents": "[]",
                "plan": json.dumps([], ensure_ascii=False),
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

            fields = _sanitize_project_patch_fields(data)
            fields["updated_at"] = datetime.now().isoformat()

            # ВАЖНО: в API v3 поле называется "id" (с маленькой буквы), а не "Id"
            payload = [{"id": project_id, "fields": fields}]

            logger.debug(
                "?? PATCH project %s keys=%s size≈%s",
                project_id,
                list(fields.keys()),
                len(json.dumps(fields, ensure_ascii=False, default=str)),
            )

            response = nocodb_request(
                "PATCH", self.projects_url, json_body=payload, headers=self.headers
            )

            if response.status_code >= 400:
                logger.error(
                    "? Ошибка обновления проекта: %s — %s",
                    response.status_code,
                    response.text[:500],
                )
                return False

            logger.debug("Проект обновлён: %s", project_id)
            return True
        except _NOCODB_REQUEST_ERRORS as e:
            logger.error(f"? Ошибка обновления проекта: {e}")
            return False

    def update_project_resilient(
        self,
        project_id: str,
        data: Dict[str, Any],
        *,
        optional_fields: Optional[Tuple[str, ...]] = None,
    ) -> bool:
        """PATCH проекта с поэтапным исключением проблемных полей (SQLite/NocoDB 422).

        optional_fields по умолчанию: iteration → plan → metrics → …
        Минимальный набор (status/tokens) пробуем в конце.
        """
        base = _sanitize_project_patch_fields(data)
        drop_order = optional_fields or (
            "iteration",
            "plan",
            "metrics",
            "excluded_agents",
            "reasoning",
            "completed_at",
            "final_report",
        )
        attempt = dict(base)
        if self.update_project(project_id, attempt):
            return True

        for key in drop_order:
            if key not in attempt:
                continue
            attempt = {k: v for k, v in attempt.items() if k != key}
            logger.warning(
                "⚠️ update_project #%s: повтор без поля «%s» (осталось %s)",
                project_id, key, list(attempt.keys()),
            )
            if self.update_project(project_id, attempt):
                return True

        # Последний шанс — только статус / токены
        minimal = {
            k: base[k]
            for k in ("status", "tokens_used", "token_budget")
            if k in base
        }
        if minimal and self.update_project(project_id, minimal):
            logger.warning(
                "⚠️ update_project #%s: сохранён только минимальный набор %s",
                project_id, list(minimal.keys()),
            )
            return True
        return False

    def get_recent_records_by_project(self, project_id: int, limit: int = 10) -> List[Dict[str, Any]]:
        """Последние записи projects с фильтром project_id (если поле есть)."""
        try:
            url = build_records_list_url(
                self.projects_url,
                where=f"(project_id,eq,{project_id})",
                page=1,
                page_size=limit,
            )
            response = nocodb_request("GET", url, headers=self.headers)
            response.raise_for_status()
            records = [
                unpack_data_record(r)
                for r in extract_records_payload(response.json())
            ]
            logger.debug("Получено %s записей для проекта %s", len(records), project_id)
            return records
        except _NOCODB_REQUEST_ERRORS as e:
            logger.error(f" Ошибка чтения записей проекта {project_id}: {e}")
            return []

    def find_project_by_id(self, project_id: int) -> Optional[Dict[str, Any]]:
        """GET /records/{recordId} — DataReadResponseV3."""
        try:
            url = f"{self.projects_url}/{project_id}"
            response = nocodb_request("GET", url, headers=self.headers)

            if response.status_code != 200:
                logger.error(
                    f" NocoDB вернул {response.status_code}: {response.text[:200]}"
                )
                return None

            project = self._unpack_record(response.json())
            logger.info(
                f"📂 Найден проект по ID: {project.get('project_name')} "
                f"(ID: {project.get('Id')})"
            )
            return project
        except _NOCODB_REQUEST_ERRORS as e:
            logger.error(f"❌ Ошибка поиска проекта: {e}")
            return None


class TasksClient:
    """Клиент для работы с таблицей tasks (Data API v3)."""

    def __init__(self):
        self.tasks_url = Config.get_nocodb_tasks_url()
        self.headers = xc_headers()
        logger.info(f"🔌 TasksClient инициализирован: {self.tasks_url}")

    def _unpack_task(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        """Распаковывает DataRecordV3 задачи."""
        return unpack_data_record(raw)

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
            
            response = nocodb_request("POST", self.tasks_url, json_body=payload, headers=self.headers)
            
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
                
        except _NOCODB_REQUEST_ERRORS as e:
            logger.error(f"❌ Ошибка создания задачи: {e}")
            if hasattr(e, 'response') and e.response is not None:
                logger.error(f"Response: {e.response.text[:300]}")
            return None

    def get_tasks_by_project(self, project_id: int) -> List[Dict[str, Any]]:
        """Получает задачи проекта (pageSize до 200; при next — доп. страницы)."""
        try:
            tasks: List[Dict[str, Any]] = []
            page = 1
            page_size = 100
            while page <= 20:  # защита от бесконечного цикла
                url = build_records_list_url(
                    self.tasks_url,
                    where=f"(project_id,eq,{project_id})",
                    page=page,
                    page_size=page_size,
                )
                response = nocodb_request("GET", url, headers=self.headers)
                if response.status_code != 200:
                    logger.error(
                        f"❌ Ошибка чтения задач: {response.status_code} — "
                        f"{response.text[:200]}"
                    )
                    break
                payload = response.json()
                raw_records = extract_records_payload(payload)
                if page == 1 and raw_records:
                    logger.debug(
                        "🔍 Первая запись задачи: %s",
                        json.dumps(raw_records[0], ensure_ascii=False)[:300],
                    )
                for r in raw_records:
                    tasks.append(self._unpack_task(r))
                # DataListResponseV3.next — URL или null; без next останавливаемся
                if not payload.get("next") or len(raw_records) < page_size:
                    break
                page += 1

            logger.debug("📥 Получено %s задач для проекта %s", len(tasks), project_id)
            tasks_without_id = [t for t in tasks if not t.get("Id")]
            if tasks_without_id:
                logger.warning(
                    f"⚠️ {len(tasks_without_id)} задач без Id! "
                    "Это приведёт к ошибкам обновления."
                )
            return tasks
        except _NOCODB_REQUEST_ERRORS as e:
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
            
            logger.debug("📝 PATCH задача %s: %s", task_id, data)
            logger.debug(f"📝 PATCH payload: {json.dumps(payload, ensure_ascii=False)[:300]}")
            
            response = nocodb_request("PATCH", self.tasks_url, json_body=payload, headers=self.headers)
            
            if response.status_code >= 400:
                logger.error(f"❌ Ошибка обновления задачи {task_id}: {response.status_code} — {response.text[:300]}")
                return False
            
            logger.debug("✅ Задача обновлена: %s", task_id)
            return True
        except _NOCODB_REQUEST_ERRORS as e:
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
            response = nocodb_request("GET", url, headers=self.headers)
            response.raise_for_status()
            data = response.json()
            return self._unpack_task(data)
        except _NOCODB_REQUEST_ERRORS as e:
            logger.error(f"❌ Ошибка чтения задачи {task_id}: {e}")
            return None

    def check_all_subtasks_completed(self, parent_task_id: str) -> bool:
        """Проверяет, завершены ли все подзадачи для родительской задачи"""
        try:
            # Ищем все подзадачи, которые зависят от parent_task_id
            url = build_records_list_url(
                self.tasks_url,
                where=f"(depends_on,like,%{parent_task_id}%)",
                page=1,
                page_size=100,
            )
            response = nocodb_request("GET", url, headers=self.headers)
            response.raise_for_status()
            raw_records = extract_records_payload(response.json())

            if not raw_records:
                # Нет подзадач — значит это не родительская задача
                return True

            for r in raw_records:
                task = unpack_data_record(r)
                if task.get("status") != "completed":
                    return False

            return True
        except Exception as e:
            logger.error(f"Ошибка проверки подзадач: {e}")
            return False
