# AI Agency OS — Архитектура и описание проекта

**Дата обновления:** Jul 7, 2026 | **Версия:** 2.1 | **Язык:** Python 3.10+

> Документ отражает состояние после рефакторинга Directions A–G.
> HTTP-слой: **FastAPI** (async endpoints, OpenAPI `/docs`). Дашборд пока vanilla (ТЗ №5 — следующий этап).

---

## 1. Назначение проекта

**AI Agency OS** — оркестратор из 10 специализированных ИИ-агентов (паттерн Supervisor-Workers) для автоматизации e-commerce (селлеры Wildberries/Ozon) и монетизации (поиск клиентов через Google + УТП).

**Основной сценарий:**
1. **PM** (Project Manager) анализирует цель проекта и строит Task Graph с зависимостями
2. **Рабочие агенты** выполняют задачи параллельно или последовательно по графу
3. **QA Gate** проверяет каждый результат (до 3 итераций на задачу)
4. **PM** финализирует проект, формируя markdown-отчёт для клиента

**Инфраструктура:**
- **Хранение состояния:** NocoDB (3 таблицы: projects, tasks, agent_logs) — stateless, допускает перезапуск
- **LLM:** Yandex AI Studio (Responses API, модель YandexGPT)
- **Frontend:** Vanilla HTML/JS дашборд + marked.js для рендеринга отчётов
- **Framework:** FastAPI + uvicorn, async HTTP; Orchestrator.run() в фоне через `asyncio.to_thread`

---

## 2. Структура репозитория

```
ai_agency_aikexpert/
├── ANALYSIS.md                  # ← этот файл
├── README.md
├── .cursorrules                 # Python best practices (LlamaFarm-style)
├── .skills/                     # Skill-файлы (patterns.md, async.md, typing.md…)
└── www/ai_agency/
    ├── main.py                  # FastAPI API (async) + nocodb_proxy
    ├── index.html               # Дашборд мониторинга (vanilla; ТЗ №5 → React)
    ├── requirements.txt         # fastapi, uvicorn, …
    ├── pytest.ini
    ├── core/
    │   ├── orchestrator.py      # ⭐ Главный цикл, task graph, deadlock, финализация
    │   ├── task_executor.py     # Выполнение одной задачи (промпт → LLM → QA)
    │   ├── qa_gate.py           # QA-проверка результатов агентов
    │   ├── agent_handlers.py    # Спец-хендлеры для 4 агентов
    │   ├── api_schemas.py       # ★ [NEW] Pydantic-схемы HTTP API
    │   ├── api_payloads.py      # ★ [NEW] Сборка payload дашборда/артефактов
    │   ├── schemas.py           # ⭐ Pydantic-модели всех агентов + call_and_parse_llm
    │   ├── nocodb.py            # Клиенты NocoDB (3 таблицы: logs, projects, tasks)
    │   ├── utils.py             # LLM API wrapper, промпты, логирование
    │   ├── config.py            # Настройки из .env
    │   ├── lead_tools.py        # Реальный поиск лидов (скрапинг / Telegram API)
    │   ├── prompt_builder.py    # build_prompt_with_schema (тонкая обёртка над schemas)
    │   └── llm_parser.py        # Тонкий ре-экспорт call_and_parse_llm из schemas
    ├── prompts/                 # 9 системных промптов (.txt)
    │   ├── pm_prompt.txt        # 5 типов ответов PM (decision, task_graph, decompose…)
    │   ├── analyst_prompt.txt
    │   ├── architect_prompt.txt
    │   ├── developer_prompt.txt
    │   ├── crm_customizer_prompt.txt
    │   ├── qa_prompt.txt
    │   ├── tech_writer_prompt.txt
    │   ├── lead_hunter_prompt.txt
    │   └── sales_prompt.txt
    └── tests/
        ├── conftest.py          # Pytest fixtures (248 строк)
        ├── test_orchestrator.py # Тесты оркестратора (522 строки)
        ├── test_schemas.py      # Тесты Pydantic-моделей (803 строки)
        ├── test_nocodb.py       # ★ [NEW] Тесты NocoDB-клиентов + прокси (345 строк)
        ├── test_prompts.py      # ★ [NEW] Тесты соответствия промптов схемам (411 строк)
        ├── test_llm.py          # Тесты call_llm (168 строк)
        └── test_qa.py           # Тесты QA Gate (118 строк)
```

---

## 3. Карта модулей

| Модуль | Строк | Роль | Статус |
|--------|-------|------|--------|
| **orchestrator.py** | **822** | Цикл, task graph, deadlock, финализация | 🔴 КРИТИЧНЫЙ |
| **schemas.py** | **955** | Pydantic-модели, `call_and_parse_llm`, AGENT_MODELS | 🔴 КРИТИЧНЫЙ |
| **agent_handlers.py** | **450** | Спец-хендлеры + обработка blueprint: architect, analyst, lead_hunter, sales | 🟠 ВЫСОКИЙ |
| **nocodb.py** | **491** | 3 NocoDB-клиента (NocoDBClient, ProjectsClient, TasksClient) | 🟠 ВЫСОКИЙ |
| **utils.py** | **258** | `call_llm`, `load_prompt`, `log_to_agent_logs` | 🟠 ВЫСОКИЙ |
| **main.py** | **~700** | FastAPI HTTP-слой, `nocodb_proxy`, OpenAPI `/docs` | 🟠 ВЫСОКИЙ |
| **api_schemas.py** | **~90** | Pydantic-схемы REST запросов/ответов | 🟢 НИЗКИЙ |
| **api_payloads.py** | **~400** | Payload дашборда и markdown-артефактов | 🟡 СРЕДНИЙ |
| **task_executor.py** | **234** | `TaskExecutor.execute()`, `_build_schema_prompt` | 🟠 ВЫСОКИЙ |
| **qa_gate.py** | **201** | `QAGate.run()` — LLM-валидация результатов | 🟠 ВЫСОКИЙ |
| **index.html** | **1037** | Дашборд (vanilla JS, real-time updates) | 🟡 СРЕДНИЙ |
| **lead_tools.py** | **314** | Telegram, WB, Ozon, Google, Avito скрапинг | 🟡 СРЕДНИЙ |
| **prompt_builder.py** | **50** | Тонкая обёртка, импортирует из schemas | 🟢 НИЗКИЙ |
| **llm_parser.py** | **54** | Ре-экспорт `call_and_parse_llm` из schemas | 🟢 НИЗКИЙ |
| **config.py** | **48** | Загрузка .env переменных | 🟢 НИЗКИЙ |

**Тестовое покрытие:** 141 тест в 6 файлах.

---

## 4. Архитектура

### Диаграмма потока (актуальная)

```
┌─────────────────────────────────────────────────────────────┐
│                  Dashboard (index.html)                     │
│  • Real-time статус задач (polling 10с)                     │
│  • История запусков: список проектов → карточки + отчёт     │
│  • Артефакты: architecture / tech_writer / crm → скачать.md │
│  • Human-review: комментарии при needs_human_review         │
│  • Парсинг output_data + сохранение n8n workflows           │
│  • Markdown-рендеринг финального отчёта                     │
└────────────────────┬────────────────────────────────────────┘
                     │ HTTP (polling)
                     ▼
       ┌──────────────────────────────┐
       │   FastAPI API (main.py)      │
       │  GET  /api/agency/status     │
       │  GET  /api/agency/projects   │
       │  GET  /api/agency/projects/{id}
       │  POST /api/agency/start|stop|resume
       │  POST /api/agency/human-review
       │  GET/POST /api/agency/workflows
       │  GET/POST/PATCH /api/nocodb
       │  /docs  /redoc  (OpenAPI)
       └────────────┬─────────────────┘
                    │ asyncio.to_thread(orchestrator.run)
                    ▼
       ┌────────────────────────────────────────────────────┐
       │                  Orchestrator                      │
       │  initialize() → _create_task_graph() → run()       │
       │                                                    │
       │  ┌──────────────┐  ┌────────────┐  ┌───────────┐  │
       │  │ TaskExecutor │  │  QAGate    │  │  Agent-   │  │
       │  │ execute()    │→ │  run()     │  │  Handlers │  │
       │  └──────────────┘  └────────────┘  └───────────┘  │
       └───────────┬──────────────────┬──────────────────────┘
                   │                  │
        ┌──────────▼──────┐  ┌────────▼────────┐
        │  schemas.py     │  │    NocoDB        │
        │  call_and_parse │  │  (REST API v3)   │
        │  _llm()         │  │  projects/tasks  │
        │  AGENT_MODELS   │  │  /agent_logs     │
        └──────────┬──────┘  └─────────────────┘
                   │
        ┌──────────▼──────┐
        │  Yandex AI      │
        │  (LLM Responses │
        │   API)          │
        └─────────────────┘
```

### Поток выполнения задачи

```
Orchestrator.run()
  └─ TaskExecutor.execute(task)
       ├─ load_prompt(agent_name)          # кэширование
       ├─ _build_schema_prompt()           # добавляет JSON Schema если нет в промпте
       ├─ build_agent_task()              # обогащает input_data зависимостями
       ├─ call_and_parse_llm()            # LLM → Pydantic-валидация → retry
       └─ DISPATCH:
            ├─ "architect"    → AgentHandlers.handle_architect()
            │                     └─ QAGate.run() + PM-декомпозиция → dev_* задачи
            ├─ "analyst"      → AgentHandlers.handle_analyst()
            │                     └─ сохраняет ROI + handoff_to_architect в контекст
            ├─ "lead_hunter"  → AgentHandlers.handle_lead_hunter()
            │                     └─ сохраняет лиды + handoff_to_sales в контекст
            ├─ "sales"        → AgentHandlers.handle_sales()
            │                     └─ сохраняет сообщения в контекст
            ├─ "qa"           → completed (без QA Gate — рекурсия!)
            └─ остальные      → QAGate.run()
                                  └─ approved → completed | rejected → pending/failed
```

---

## 5. Модель данных (NocoDB)

### Таблица `projects`

| Поле | Тип | Описание |
|------|-----|----------|
| `id` | int | ID записи (API v3, lowercase!) |
| `project_name` | str | Название проекта |
| `client_name` | str | Имя клиента |
| `goal` | text | Цель автоматизации |
| `status` | enum | `in_progress` \| `completed` \| `stopped` \| `needs_human_review` |
| `current_phase` | str | `lead_gen` \| `sales` \| `analysis` \| `architecture` \| `development` \| `qa` \| `documentation` |
| `tokens_used` | int | Использовано токенов |
| `token_budget` | int | Лимит токенов |
| `completed_agents` | JSON | JSON-массив имён завершённых агентов |
| `final_report` | text | Markdown-отчёт |
| `metrics` | JSON | `{total_tokens, tasks_completed, tokens_per_agent}` |
| `updated_at` | datetime | Автоматически при каждом PATCH |

### Таблица `tasks`

| Поле | Тип | Описание |
|------|-----|----------|
| `id` | int | ID записи (API v3, lowercase!) |
| `task_id` | str | Логический ID: `task_001`, `dev_001`… |
| `project_id` | int | FK → projects.id |
| `agent_name` | str | Имя агента-исполнителя |
| `task_description` | text | Постановка задачи |
| `input_data` | JSON | Обогащённый контекст (goal, depends_on outputs, leads_context…) |
| `output_data` | JSON | Ответ агента (Pydantic-модель → JSON) |
| `status` | enum | `pending` \| `in_progress` \| `completed` \| `failed` \| `needs_human_review` |
| `depends_on` | JSON | Массив `task_id` зависимостей |
| `qa_approved` | str | `pending` \| `true` \| `false` |
| `qa_feedback` | text | Обратная связь от QA Gate |
| `iteration_count` | int | Текущий номер итерации |
| `max_iterations` | int | Лимит итераций (default 3) |
| `tokens_used` | int | Токены, потраченные задачей |
| `updated_at` | datetime | Автоматически при каждом PATCH |

### Таблица `agent_logs`

| Поле | Тип | Описание |
|------|-----|----------|
| `agent_name` | str | Имя агента |
| `project_id` | int | FK → projects |
| `status` | str | `review` \| `completed` \| `failed` |
| `task_description` | text | Краткое описание |
| `full_response` | text | JSON ответа агента |
| `tokens_used` | int | Метрика |

> **Важно:** NocoDB API v3 использует `id` (lowercase) для идентификатора записи в PATCH/PUT payload.
> Внутри кода для единообразия запись нормализуется в `fields["Id"]` через `_unpack_record`/`_unpack_task`.

---

## 6. Главный цикл оркестрации

**Файл:** `orchestrator.py`, метод `run()`.

### Последовательность

```
initialize(project_id=… | create={…})
  ├─ create={project_name, client_name, goal, token_budget?, current_phase?}
  │    → предыдущий in_progress → stopped → create_project() из формы
  ├─ project_id → загрузить конкретный проект
  ├─ Найти in_progress/stopped/needs_human_review → загрузить / resume
  └─ Не найден → create_project() из Config defaults

_create_initial_task_graph()
  ├─ PM вызывается с PMTaskGraph (excluded_agents + reasoning + tasks)
  └─ Задачи создаются в NocoDB

run() — главный цикл:
  while agency_running and iteration < MAX_TOTAL_ITERATIONS:
    ├─ Проверка бюджета токенов → **stopped**, если осталось < 10% бюджета
    ├─ Загрузка задач из NocoDB
    ├─ Восстановление зависших in_progress → pending (> 5 минут)
    ├─ Проверка: все completed? → finalize()
    ├─ Поиск готовых задач (depends_on ⊆ completed_ids)
    ├─ Если нет готовых задач → resolve_deadlock()
    └─ execute_task(task) через TaskExecutor

resolve_deadlock()
  ├─ PM анализирует ситуацию
  └─ Решение: update_dependencies | skip_tasks | create_tasks |
              stop_project | need_human_review

finalize()
  └─ PM формирует PMFinalReport (markdown-отчёт + метрики)
```

---

## 7. QA Gate

**Файл:** `qa_gate.py`, класс `QAGate`.

### Поток

```
QAGate.run(task, agent_response, iteration_count, max_iter)
  ├─ call_and_parse_llm(QAResponse)  ← отдельный LLM-вызов
  ├─ APPROVED если: tests_failed == 0 AND нет critical/high issues
  ├─ APPROVED → update_task(status="completed", qa_approved="true")
  └─ REJECTED:
       ├─ iteration_count + 1 < max_iter → status="pending" + qa_feedback
       └─ иначе → status="failed"
```

**Критерии QA Response:**
- `tests_failed == 0` **и** ни одной issue с `severity ∈ {critical, high}`

---

## 8. Спецобработчики агентов

**Файл:** `agent_handlers.py`, класс `AgentHandlers`.

### Почему не QA Gate?

| Агент | Маршрут | Причина |
|-------|---------|---------|
| `analyst` | Спец-хендлер | Накапливает ROI/боли + `handoff_to_architect` в `analyst_context` |
| `lead_hunter` | Спец-хендлер | Накапливает лиды + `handoff_to_sales`; fallback к Telegram API |
| `sales` | Спец-хендлер | Накапливает сообщения/квалификацию в `sales_context` |
| `architect` | Спец-хендлер | QA Gate + второй LLM-вызов: PM декомпозирует на `dev_*` подзадачи |
| `developer`, `crm_customizer`, `tech_writer` | **QA Gate** | Самодостаточные артефакты, только валидация |
| `qa` | Прямое завершение | Не может проверять сам себя (бесконечная рекурсия) |

### Контекстный поток (handoff-протокол)

```
client_hunter → client_hunter_context + client_hunter_handoff_to_sales
lead_hunter   → leads_context + leads_handoff_to_sales
                     ↓  (sales обязателен)
sales         → sales_context  (тексты сообщений)
                     ↓
analyst       → analyst_context + handoff_to_architect
                     ↓
architect     → QA Gate → PM декомпозиция → dev_001…dev_N
                     ↓
developer     → QA Gate (каждая dev_*; output_data → qa через dependency_outputs)
                     ↓
qa            → completed
                     ↓
tech_writer   → QA Gate
```

Каждая `dev_*` проверяется **QA Gate** в `TaskExecutor` (отдельные задачи агента `qa` на сабтаск не создаются). Финальный агент `qa` из task graph ждёт завершения placeholder developer (после всех `dev_*`).

Все накопленные контексты (`leads_context`, `sales_context`, `analyst_context`) автоматически добавляются в `input_data` последующих задач через `_build_enriched_input_data`.

---

## 9. Pydantic-модели агентов

**Файл:** `schemas.py`.

### Карта моделей

| Агент/тип | Класс | Ключевые поля |
|-----------|-------|---------------|
| PM решение | `PMDecision` | `project_status`, `next_agent`, `task_for_next_agent` |
| PM task graph | `PMTaskGraph` | `tasks[]`, `excluded_agents[]`, `reasoning` |
| PM декомпозиция | `PMDecomposition` | `subtasks[]`, `pm_comment` |
| PM финальный отчёт | `PMFinalReport` | `final_report` (markdown), `metrics{}` |
| PM human review | `PMHumanReview` | `updated_task_description` |
| PM deadlock | `PMDeadlockResolution` | `solution` (5 вариантов), `actions[]` |
| Analyst | `AnalystResponse` | `roi_calculation`, `current_pain_points[]`, **`handoff_to_architect`** |
| Architect | `ArchitectResponse` | `systems[]`, `data_flow[]`, `tech_stack[]` |
| Developer | `DeveloperResponse` | `n8n_json`, `files[]`, `setup_instructions[]` |
| QA | `QAResponse` | `tests_total/passed/failed`, `issues[]`, `test_cases[]` |
| Tech Writer | `TechWriterResponse` | `documents[]`, `video_scripts[]`, `faq[]` |
| Lead Hunter | `LeadHunterResponse` | `leads_found[]`, `total_found`, **`handoff_to_sales`** |
| Sales | `SalesResponse` | `messages[]`, `qualification_questions[]` |
| CRM Customizer | `CRMCustomizerResponse` | `entities[]`, `pipelines[]`, `field_mapping[]` |

### Ключевые решения дизайна

- **`extra="ignore"`** (Pydantic v2 default) для всех агентных моделей — LLM может генерировать лишние поля без ошибки
- **`handoff_to_*`** поля — Optional[Dict], позволяют LLM передавать структурированные рекомендации следующему агенту (analyst → architect, lead_hunter → sales)
- **`DataFlowStep`** использует aliases `from`/`to` (reserved keywords в Python) через `populate_by_name=True`
- **`call_and_parse_llm`** — единственная реализация парсинга: extract JSON → Pydantic validation → retry с qa_feedback

---

## 10. NocoDB-слой

**Файл:** `nocodb.py`.

### Три клиента

| Класс | Таблица | Основные методы |
|-------|---------|-----------------|
| `NocoDBClient` | `agent_logs` | `create_record`, `get_recent_records` |
| `ProjectsClient` | `projects` | `find_project_by_name`, `get_or_create_project`, `update_project` |
| `TasksClient` | `tasks` | `create_task`, `get_tasks_by_project`, `update_task`, `get_task_by_id` |

### Надёжность HTTP (`nocodb_request`) — Direction Y

Все read/write к NocoDB идут через `nocodb_request()`:

| Параметр | Config / env | Default |
|----------|--------------|---------|
| Таймаут запроса | `NOCODB_TIMEOUT_SEC` | **60** с |
| Число попыток | `NOCODB_MAX_ATTEMPTS` | **4** (1 + 3 повтора) |
| Паузы между попытками | `NOCODB_RETRY_DELAYS_SEC` | **10, 30, 60** с |

Ретраи только при временных сбоях: сеть, timeout, HTTP **429** / **5xx**.
Клиентские **4xx** не повторяются. После исчерпания попыток — `NocoDBTransientError`
(вызывающий код по-прежнему возвращает `None` / `False` / `[]`).

### API v2 vs API v3

- **API v3** использует `id` (lowercase) в PATCH payload: `[{"id": 42, "fields": {...}}]`
- **API v2** использовал `Id` (capitalize)
- `_unpack_record` / `_unpack_task` нормализуют цепочку `id → Id → row_id` в `fields["Id"]` для обратной совместимости

### nocodb_proxy (main.py)

Защищён тремя слоями:
```python
# Разрешённые методы (DELETE исключён)
_PROXY_ALLOWED_METHODS = frozenset({'GET', 'POST', 'PATCH'})

# Разрешённые пути: только пустой или числовой ID записи
_PROXY_ALLOWED_PATH_RE = re.compile(r'^(\d+)?$')

# Разрешённые query-параметры
_PROXY_ALLOWED_PARAMS = frozenset({'limit', 'offset', 'where', 'sort'})
```

OpenAPI: отдельные `operation_id` на каждый метод×путь (`nocodb_proxy_root_get`, …
`nocodb_proxy_path_patch`) — иначе FastAPI предупреждает о Duplicate Operation ID
при `methods=[GET,POST,PATCH]` на одном `api_route`.

---

## 11. Промпты и соответствие схемам

**Папка:** `prompts/*.txt`

| Промпт | Схема вшита | Соответствует Pydantic | Примечание |
|--------|-------------|----------------------|------------|
| `pm_prompt.txt` | ❌ (динамически) | ✅ 5 типов PM-ответов | Содержит все 5 типов: decision, graph, decompose, finalize, deadlock |
| `analyst_prompt.txt` | ✅ | ✅ `AnalystResponse` | Handoff-протокол для architect |
| `architect_prompt.txt` | ✅ | ✅ `ArchitectResponse` | Handoff-протокол для developer |
| `developer_prompt.txt` | ✅ | ✅ `DeveloperResponse` | Handoff-протокол для qa |
| `qa_prompt.txt` | ❌ (динамически) | ✅ `QAResponse` | |
| `tech_writer_prompt.txt` | ✅ | ✅ `TechWriterResponse` | `integration_guide` + чек-лист интеграции (Direction W) |
| `lead_hunter_prompt.txt` | ✅ | ✅ `LeadHunterResponse` | |
| `sales_prompt.txt` | ✅ | ✅ `SalesResponse` | Исправлена неверная идентичность роли |
| `crm_customizer_prompt.txt` | ✅ | ✅ `CRMCustomizerResponse` | |

**Логика добавления схемы в промпт** (`TaskExecutor._build_schema_prompt`):
- Если промпт уже содержит `"СТРОГАЯ СТРУКТУРА ОТВЕТА"` — не добавляет (7 промптов)
- Иначе — динамически генерирует из Pydantic-модели (2 промпта: pm, qa)

---

## 12. Что было исправлено (Directions A–E)

### Direction A+B — Рефакторинг оркестратора и парсинга LLM

| До | После |
|----|-------|
| `orchestrator.py` 1657 строк | `orchestrator.py` 822 строки |
| — | `task_executor.py` 234 строки |
| — | `qa_gate.py` 201 строка |
| — | `agent_handlers.py` 405 строк |
| 3 копии парсинга LLM | 1 каноническая `call_and_parse_llm` в `schemas.py` |
| `llm_parser.py` 130 строк (дубль) | 54 строки (ре-экспорт) |
| `prompt_builder.py` дублировал `AGENT_MODELS` | Импортирует из `schemas` |

### Direction C — Модели данных и валидация

- **`PMTaskGraph`**: добавлены `excluded_agents: List[str]` и `reasoning: str`
- **`Document.type`**: добавлен `"commercial_proposal"` в Literal
- **`AGENT_MODELS`**: словарь централизован в `schemas.py`, удалён дубль из `prompt_builder.py`
- **`get_model_example`**: расширен до полных примеров для всех 14 моделей
- **`DataFlowStep`**: aliases `from`/`to` + `populate_by_name=True`

### Direction D — Persistence / NocoDB

- **Баг `_build_where_url`**: `return url` был внутри `if sort_field:` → возвращал `None`
- **Баг `find_project_by_status`**: `if False` + `{field}` NameError → переписан через `_build_where_url`
- **Баг `get_recent_records_by_project`**: `self.records_url` → `self.projects_url` (AttributeError)
- **`nocodb_proxy`**: убран DELETE, добавлены path/param whitelist, regex-валидация пути
- **`main.py`**: удалён мёртвый импорт `validate_with_qa`
- **Добавлены** 28 тестов в `test_nocodb.py`

### Direction E — Промпты и поведение агентов

- **`sales_prompt.txt`**: исправлена неверная идентичность роли (копипаста Analyst)
- **`PMDeadlockResolution.solution`**: добавлен `"need_human_review"` в Literal
- **`AnalystResponse`**: добавлено поле `handoff_to_architect: Optional[Dict]`
- **`LeadHunterResponse`**: добавлено поле `handoff_to_sales: Optional[Dict]`
- **`agent_handlers.py`**: хендлеры сохраняют handoff-данные в `current_project`
- **Документация** в `agent_handlers.py`: объяснение почему 4 агента имеют спец-хендлеры
- **Добавлены** 28 тестов в `test_prompts.py`

### Direction F — Validating n8n workflows + Architect blueprint (NEW)

**Проблема:** Developer генерирует n8n JSON с системными дефектами (несоединённые ноды, пустые условия IF, неверные credentials) из-за разрыва между концептуальным `data_flow` архитектора и конкретной сериализацией.

**Решение:** Встроить функцию системного аналитика в архитектора через node-level `workflow_blueprint`. Отдельный агент не нужен (добавил бы LLM-вызов и точку потери контекста без пропорциональной выгоды).

**Изменения:**
- **`schemas.py`**: добавлено поле `handoff_to_developer: Optional[Dict]` в `ArchitectResponse` (содержит workflow_blueprint с nodes[], connections[], field_mapping[], error_handling[])
- **`agent_handlers.py`**: 
  - `handle_architect` извлекает blueprint из `handoff_to_developer`
  - blueprint передаётся **целиком** в `input_data` каждой dev-подзадачи (баг: раньше архитектура обрезалась до 2000 символов)
  - PM при декомпозиции привязывает подзадачи к конкретным нодам blueprint
- **`architect_prompt.txt`**: Раздел «WORKFLOW BLUEPRINT — ДЕТАЛЬНАЯ СПЕЦИФИКАЦИЯ НОД» с правилами (nodes/connections с branch для IF/switch, field_mapping, error_handling)
- **`developer_prompt.txt`**: Раздел «WORKFLOW_BLUEPRINT — SOURCE OF TRUTH» (developer сериализует blueprint, не проектирует; явные правила про `index`, непустые условия IF, `fieldsUi`)
- **`validate-n8n.js` + `core/n8n_validator.py`**: Проверки на `inputIndex` (ошибка: ноды не соединяются), изолированные ноды, неверные credentials ключи, пустой `data:{}` в nocoDb update
- **`developer_prompt.txt`** (раздел КРИТИЧНО): Шаблоны NocoDB update/connections/credentials, расширенный чеклист (8 пунктов вместо 4)

**Результаты:** 94 теста проходят. Валидатор ловит все дефекты в тестовом workflow (5 классов).

### Direction N — Настройка n8n-validator для корректной разработки (NEW)

**Проблема:** валидатор был «наполовину настроен»:
1. `node_modules` / официальный `n8n-workflow-validator` часто не ставится (SheetJS CDN).
2. Heuristic-замечания после «чистого» runtime **не блокировали** developer
   (`validate_n8n_workflow` возвращал `is_valid=True` с issues → `task_executor` их игнорировал).
3. Не хватало критичных проверок из developer-чеклиста: пустой IF, отсутствие `index`,
   обе ветки IF, дубли имён, webhook.path, fieldsUi у NocoDB update.
4. Баг в `validate-n8n.js`: `params.options === {}` никогда не срабатывал (сравнение по ссылке).

**Решение — двухуровневый gate, heuristic всегда блокирует:**

| Слой | Файл | Роль |
|------|------|------|
| Runtime | `validate-n8n.js` (без npm) | Локальный Node-скрипт; `--json` для Python |
| Heuristic | `core/n8n_validator.py` | Всегда обязателен; ERROR → `is_valid=False` |
| Опционально | `n8n-workflow-validator` (npm) | Official engine, если удалось установить |

**Что ловит (блокирует developer → pending/failed + qa_feedback):**
- `scheduleTrigger.rule.interval` не массив («is not iterable»)
- IF v2: нет/`[]`/пустой `leftValue`/нет `operator`
- IF в connections без обеих веток (`main` < 2)
- `inputIndex` вместо `index`, отсутствие `index`
- изолированные ноды / trigger без исходящих связей
- пустой `options:{}` у if/switch
- NocoDB update: `data:{}` или нет `fieldsUi.fieldValues`
- неверные credential keys, дубли имён нод
- webhook без `path`, httpRequest без `url`, Set v3 с `values`

**Запуск вручную:**
```bash
cd www/ai_agency
node validate-n8n.js path/to/workflow.json
node validate-n8n.js --json path/to/workflow.json
# отключить runtime в Python (только heuristic):
set N8N_VALIDATOR_RUNTIME=off
```

**Тесты:** `tests/test_n8n_validator.py` — happy-path + все import-killers.
В пайплайне developer: `TaskExecutor` → `validate_n8n_workflow` → при fail возвращает
задачу в `pending` с `build_n8n_feedback(issues)` (до `DEVELOPER_MAX_ITERATIONS`).

### Direction O — Official n8n-engine validator (внешний шаг) (NEW)

**Запрос:** эвристики недостаточно для максимальной точности — подключить официальный
валидатор / MCP как внешний шаг.

**Что подключено (Layer C):** пакет
[`n8n-workflow-validator`](https://www.npmjs.com/package/n8n-workflow-validator) —
использует реальный движок n8n (`n8n-workflow` + `n8n-nodes-base`), те же
`NodeHelpers.getNodeParameters` / issues, что редактор при импорте. Это правильный
путь для **JSON** от агента `developer`.

| Env | Значение | Поведение |
|-----|----------|-----------|
| `N8N_VALIDATOR_OFFICIAL` | `auto` (default) | Пробуем binary → global → `npx --yes`; если нет — skip без fail |
| `N8N_VALIDATOR_OFFICIAL` | `on` | Official обязателен; недоступен → `is_valid=False` |
| `N8N_VALIDATOR_OFFICIAL` | `off` | Только heuristic + local JS |
| `N8N_VALIDATOR_OFFICIAL_TIMEOUT` | `120` | Таймаут npx/binary (сек) |
| `N8N_VALIDATOR_RUNTIME` | `off` | Отключить только local `validate-n8n.js` |

**Порядок слоёв в `validate_n8n_workflow()`:**
1. **A Heuristic** (Python) — всегда, блокирует
2. **B Local JS** (`validate-n8n.js`) — если файл есть и runtime не off
3. **C Official** (`node_modules` / global / `npx --yes n8n-workflow-validator --json`) — блокирует при ERROR

Раньше local JS стоял *первым* в поиске binary и **перекрывал** official — исправлено:
official ищется отдельно и всегда вызывается при `auto|on`.

Вывод official парсится с `schemaDelta` / `n8nError` → в `qa_feedback` developer
видит missing/extra keys (как в редакторе).

**n8n Instance MCP (опционально):** официальный Builder MCP
(`validate_workflow` с n8n ≥ 2.12) принимает **TypeScript Workflow SDK code**, не raw JSON.
Для JSON-пайплайна агентства Layer C — основной. Заготовки env:
`N8N_MCP_URL`, `N8N_MCP_ACCESS_TOKEN` (в `Config`) — под будущее подключение
instance MCP, если перейдём на SDK-генерацию.

**Установка:**
```bash
cd www/ai_agency
node -v                         # обязательно >= 22 (на v20 → EBADENGINE isolated-vm)
npm run install-validator       # xlsx через overrides с npmjs (не cdn.sheetjs.com)
npx n8n-workflow-validator --json workflow.json
```

Предупреждения `deprecated uuid/gm/glob` — нормальны (зависимости n8n).
`EBADENGINE … current: node v20` — нужно обновить Node, иначе native-сборка может сломаться.

### Direction K — Устойчивый workflow (чек-лист надёжности) (NEW)

**Источник:** рекомендации по разработке устойчивых процессов автоматизации.
**Правило:** если хотя бы один пункт чек-листа не закрыт — процесс не готов к реальной нагрузке.

Агенты `architect`, `developer`, `qa` обязаны учитывать чек-лист при проектировании, сериализации n8n и проверке.

#### Чек-лист (каждый сценарий)

| # | Пункт | Что проверить |
|---|-------|---------------|
| 1 | Тип ошибки | Различаются временные (timeout, 429, 5xx) и постоянные (4xx валидации, auth). Retry только для временных. |
| 2 | Что можно повторять | Безопасность повтора; защита от дублей (idempotency_key / external_id) для внешних действий. |
| 3 | Интервал попыток | Пауза между попытками; нарастающий интервал (backoff); случайное смещение (jitter). |
| 4 | Ограничения | maxTries; лимит общего времени ожидания; нет бесконечного цикла. |
| 5 | Резервный сценарий | После исчерпания попыток: лог ошибки, уведомление ответственному, сохранение данных / компенсация. |

#### Ключевые принципы

1. Временные сбои — норма; процесс должен быть к ним готов.
2. Повторяем не всё подряд — только восстанавливаемые действия.
3. Повторы ограничены по числу попыток и по общему времени.
4. Паузы осмысленные: backoff + jitter (избегать thrashing / thundering herd).
5. Лимиты API учитываем на этапе проектирования, а не после 429.
6. Не путаем инструменты: **Wait/delay** — локальные паузы; **очередь** — управление потоком нагрузки.
7. Надёжный процесс умеет: повторять, ждать, останавливаться, уходить в резерв.

#### Где закреплено в коде агентства

| Артефакт | Роль |
|----------|------|
| `prompts/architect_prompt.txt` | Проектирование: error_handling[], rate limits, fallback в blueprint |
| `prompts/developer_prompt.txt` | Сериализация: retryOnFail, backoff/jitter, идемпотентность, Error-ветка |
| `prompts/qa_prompt.txt` + `qa_gate.py` | Приёмка по чек-листу устойчивости |
| **`n8n_validator.py` heuristic (Фаза 1)** | **ERROR** на критичных `httpRequest` (POST/PUT/PATCH/DELETE) и `nocoDb` (create/update/delete): нет `retryOnFail`+`maxTries`+`waitBetweenTries`, нет error-ветки (`onError=continueErrorOutput` / `continueOnFail`), create/POST без `external_id`/`idempotency_key`, self-loop в connections |
| Env `N8N_VALIDATOR_RESILIENCE` | `on` (default) / `off` — отключить Direction K ERROR в heuristic |

**Smoke schemaDelta:** если Layer C вернул `missingKeys` / `N8N_PARAMETER_VALIDATION_ERROR`
(даже при exit code 0) — `validate_n8n_workflow` → `is_valid=False` (developer retry).

### Direction Z — Фаза 1 roadmap: качество поставки n8n (NEW)

Реализация плана развития (§14 → Фаза 1):

1. Direction K в **коде** heuristic (не только промпты) — см. таблицу выше.
2. Layer C в CI: `.github/workflows/ci.yml` — Node **≥22**, `npm run install-validator`,
   job с `N8N_VALIDATOR_OFFICIAL=on`.
3. Smoke: блокировка по `schemaDelta.missingKeys` / parameter ERROR.
4. Instance MCP / SDK-генерация — **не** в этой фазе (заготовки `N8N_MCP_*` без изменений).

Тесты: `tests/test_n8n_validator.py` → `TestDirectionKResilience`, smoke schemaDelta.

---

### Direction AJ — Parent developer → completed после всех сабтасков (FIX)

**Регрессия:** `startswith("dev_")` не ловил `iterN_dev_*` после refine →
placeholder (`task_003` / `iterN_task_*`) оставался в `failed`, qa/tech_writer
ждали «completed» родителя.

**Исправление:** `core/task_ids.py` — `is_developer_subtask_id` /
`is_developer_placeholder_task`; `check_and_complete_parent_tasks` и
`handle_architect` используют их. После всех completed-сабтасков родитель →
`completed`.

Тесты: `tests/test_parent_task_complete.py`.

### Direction AI — Agent Context MCP для любого агента (NEW)

Расширение Direction AG: память и MCP не только для developer.

| Слой | Что |
|------|-----|
| `TaskExecutor` | system-hint `build_system_memory_hint(agent)` на retry для **всех** ролей |
| `inject_retry_into_input_data(..., agent_name=)` | нейтральная инструкция (не только workflow) |
| MCP | `list_supported_agents`, `list_agents_with_memory`, `get_agent_history`, `get_latest_context_for_agent` |
| Store API | `list_agents_with_context`, `get_agent_history`, `get_latest_task_for_agent` |

Роли: pm, developer, architect, analyst, sales, client_hunter, lead_hunter, qa,
tech_writer, crm_customizer.

### Direction AH — Исчерпание токенов → status=stopped (NEW)

Раньше при остатке бюджета < 20% проект уходил в `needs_human_review`
(путаница с human-review по замечаниям). Теперь: **`stopped`** + лог PM
`budget_exhausted`. Продолжение: увеличить бюджет («+токены») и Resume.

### Direction AG — Agent Context MCP + память итераций (NEW)

**Проблема:** developer на retry получал только `qa_feedback`, без предыдущего
`n8n_json` → каждый раз генерировал workflow «с нуля» и повторял те же ошибки.

**Решение:**

| Компонент | Роль |
|-----------|------|
| `core/agent_context.py` | JSON-store попыток: digest, preview, open_issues |
| `TaskExecutor` | на fail пишет attempt + `output_data`; на retry inject в prompt |
| `build_agent_task(..., iteration_memory=)` | блок «НЕ генерируй с нуля» |
| `agency_agent_context_mcp.py` | MCP для Cursor: get/list/record/clear context |
| `.cursor/mcp.json` | сервер `agency-agent-context` |

Инструменты MCP: `get_task_context`, `get_retry_prompt`, `list_task_contexts`,
`record_agent_attempt`, `clear_context` + (Direction AI)
`list_supported_agents`, `list_agents_with_memory`, `get_agent_history`,
`get_latest_context_for_agent`.

Store: `www/ai_agency/data/agent_context/{project_id}/{task_id}.json` (в `.gitignore`).

Тесты: `tests/test_agent_context.py`.

### Direction AE+AF — 1 workflow / task + Direction K autofix (NEW)

**Замечания с прогона (developer `dev_001_2`, 6/6):**
1. Одни и те же Direction K ERROR (retry / резервная ветка / idempotency) на всех
   итерациях — LLM не применял механический чек-лист.
2. В одной задаче developer сделал **два** Telegram workflow (агент→менеджер и
   менеджер→агент).

**Исправления:**

| Слой | Что |
|------|-----|
| `extract_workflow_units` | `workflow_blueprints[]` → N независимых сценариев |
| `normalize_developer_subtasks` | N units → N `full_workflow` (не склеивать) |
| architect / PM / developer prompts | явный контракт: 1 workflow = 1 task |
| `check_one_workflow_per_task` | >1 trigger / >1 n8n_workflow file / «два workflow» в summary → fail |
| `apply_direction_k_autofix` | до валидации патчит retry/continueOnFail/idempotency |
| `build_n8n_feedback` | обязательный JSON-рецепт Direction K + повтор-warning |

Тесты: `test_dev_decomposition.py` (multi-unit), `test_n8n_validator.py` (autofix / one-wf).

### Direction AC — Refine/итерация: SQLITE_ERROR 422 при update_project (NEW)

**Симптом:** после PM task graph на новой итерации:
`ERR_DATABASE_OP_FAILED` / `SQLITE_ERROR` / `message: "near"` (два раза подряд).
Refine всё равно мог вернуть 200 (задачи уже созданы), но статус/metrics проекта
не записались.

**Причины:**
1. `completed_at: ""` — пустая строка в DateTime ломает SQL NocoDB/SQLite.
2. Раздутый `metrics.iteration_history` (final_report до 50k × N) и огромный `plan`.
3. Опциональная колонка `iteration` — первый PATCH падал, второй тоже из‑за п.1–2.

| Исправление | Где |
|-------------|-----|
| `completed_at: null` вместо `""` | `orchestrator.start_project_iteration` + sanitize в `nocodb` |
| Архив отчёта ≤ 8k в history | `prepare_iteration_metrics` |
| Усечение/компакт `plan` | `start_project_iteration` |
| `update_project_resilient` — дроп проблемных полей | `ProjectsClient` |

Тесты: `test_empty_completed_at_sent_as_null`, `test_resilient_retries_without_failing_field`.

### Direction AB — Sales без hunter не затирает письма в tasks (NEW)

**Проблема:** после итерации «УТП → письмо клиенту» sales писал полный текст в
`agent_logs`, но в `tasks.output_data` оставалось `messages: []` («Писем: 0»).
Причина: `handle_sales` очищал все письма, если не было `client_hunter_context` /
`leads_context` (анти-галлюцинация для cold outreach). Для УТП клиент уже в
`client_name` / `analyst_context` — очистка ошибочна.

| Исправление | Где |
|-------------|-----|
| Fallback-клиент проекта / analyst | `agent_handlers.handle_sales` |
| Частичное совпадение `lead_name` | `_lead_name_allowed` |
| Один клиент + другое имя → нормализация, письма сохраняются | `handle_sales` |

Тест: `test_handle_sales_usp_without_hunter_keeps_letter`.

### Direction AA — Выгрузка результата УТП / analyst (NEW)

**Проблема:** проект «подготовка УТП» завершался (analyst + qa), но в UI не было
ни блока артефакта, ни кнопки скачивания. В шапке только «Выгрузить письма»
(outreach sales) — для УТП бесполезно. `AnalystResponse` без `summary` → пустой
`output_preview`. Финальный отчёт PM мог отсутствовать — тогда результат «пропадал».

| Исправление | Где |
|-------------|-----|
| Артефакт `usp_proposal` + markdown | `api_payloads._build_agent_artifact` / `_artifact_to_markdown` |
| Preview из ROI/клиента | `_output_preview_for_agent` |
| Download API | `GET /artifacts/download` разрешает `analyst` |
| UI | карточка analyst, блок «Результаты для скачивания», кнопка «Скачать результат .md» |
| Outreach-кнопки | скрываются, если нет client_hunter/sales |

Тесты: `tests/test_api_payloads_artifacts.py`.

### Branding — логотип, favicon, фирменные цвета (РЕАЛИЗОВАНО)

Символика отражает миссию агентства: надёжная оркестрация распределённых ИИ-агентов
и связные автоматизации. Знак — буква «A», внутри которой сетевой узел (3 точки,
соединённые линиями): агентство «соединяет» системы клиента через n8n/NocoDB/CRM.

**Файлы (`www/ai_agency/static/brand/`):**

| Файл | Назначение |
|------|------------|
| `logo.svg` | Основной логотип (символ + wordmark «AI Agency»), векторный, для дашборда |
| `logo.png` | Растровый экспорт логотипа (fallback / соцсети / документы) |
| `favicon.svg` | Векторная версия значка (современные браузеры) |
| `favicon.ico` | Мультиразмерный (16/32/48/64 px) для старых браузеров |
| `favicon-16.png`, `favicon-32.png` | PNG нужных размеров под `<link rel="icon" sizes="...">` |
| `apple-touch-icon.png` | 180×180 для iOS/закладок |
| `favicon-source.png` | Исходный квадратный растровый значок (источник для ресайза) |

**Фирменные цвета (design tokens):**

| Токен | HEX | Назначение |
|-------|-----|------------|
| `--brand-deep` | `#0f3b52` | Тёмно-синий, основной штрих символа/текст wordmark |
| `--brand-teal` | `#0f5c4c` | Акцент дашборда (уже используется как `--accent` в `index.html`) |
| `--brand-cyan` | `#18b2c4` | Вторичный акцент символа (сетевые узлы/линии, градиент) |
| `--brand-ink-on-dark` | `#f7fffb` | Текст/символ на тёмном фоне значка |

**Интеграция:**
- `main.py`: `app.mount("/static", StaticFiles(directory=_BASE_DIR / "static"), name="static")`.
- `index.html` `<head>`: `favicon.svg` → `favicon-32.png` / `favicon-16.png` → `favicon.ico`
  (браузер выбирает лучший вариант по приоритету) + `apple-touch-icon`.
- `index.html` `<header>`: `<img id="site-logo" src="/static/brand/logo.svg">` рядом с брендом.

Регенерация PNG/ICO из источника (при обновлении дизайна) — через Pillow:
`Image.open(favicon-source.png).resize((size, size), Image.LANCZOS)`, затем
`.save(..., format="ICO", sizes=[(16,16),(32,32),(48,48),(64,64)])`.

## 13. Тестовое покрытие

| Файл | Строк | Тестов | Что покрывает |
|------|-------|--------|---------------|
| `conftest.py` | 248 | — | Fixtures: mock NocoDB, sample data, Pydantic объекты |
| `test_orchestrator.py` | 522 | 17 | Инициализация, task graph, QA, спец-хендлеры |
| `test_schemas.py` | 803 | 28 | Все Pydantic-модели, парсинг JSON, AGENT_MODELS, примеры |
| `test_nocodb.py` | 345 | 28 | `_unpack`, `update_task/project`, `_build_where_url`, прокси-безопасность |
| `test_prompts.py` | 411 | 28 | Соответствие промптов схемам, handoff-поля, dispatch-логика |
| `test_llm.py` | 168 | 13 | `call_llm`, retry, truncated JSON |
| `test_qa.py` | 118 | 6 | QA Gate approve/reject/error |
| **Итого** | **2615** | **141** | **141/141 ✅** |

---

## 14. Открытые вопросы и следующие шаги

### Direction G — Миграция HTTP на FastAPI (ТЗ №3) — РЕАЛИЗОВАНО

| До | После |
|----|-------|
| Flask + flask-cors | FastAPI + CORSMiddleware |
| `threading.Thread(orchestrator.run)` | `asyncio.create_task(asyncio.to_thread(orchestrator.run))` |
| Нет OpenAPI | `/docs` (Swagger), `/redoc` |
| Ручной parse JSON body | Pydantic: `HumanReviewRequest`, `SaveWorkflowRequest`, … (`api_schemas.py`) |
| Payload helpers в main.py | `core/api_payloads.py` |
| Flask test_client | `fastapi.testclient.TestClient` + `tests/test_api.py` |

Контракт JSON для дашборда сохранён (`{"error": "..."}` через exception handler).
Блокирующие I/O (NocoDB, LLM) обёрнуты в `asyncio.to_thread`, чтобы не блокировать event loop.

### Direction I — Создание проектов с дашборда (NEW)

Механизм: ввод всех полей проекта → создание в NocoDB → запуск оркестратора.

| Слой | Что сделано |
|------|-------------|
| API | `StartProjectRequest` в `api_schemas.py`; `POST /api/agency/start` с JSON-телом |
| Orchestrator | `initialize(create={…})` — всегда новый проект; активный → `stopped` |
| NocoDB | `ProjectsClient.create_project(..., current_phase=)` |
| UI | Панель «Новый проект»: name, client, goal, budget, phase → «Создать и запустить» |

```http
POST /api/agency/start
Content-Type: application/json

{
  "project_name": "Автоматизация заявок",
  "client_name": "ООО ТехноФикс",
  "goal": "…(мин. 10 символов)",
  "token_budget": 30000,
  "current_phase": "lead_gen"
}
```

Пустое тело — обратная совместимость (resume / defaults из `Config`).
Если цикл уже `running` и передано тело формы — сначала `stop`, затем создание нового проекта.

### Direction J — Монетизация: агент `client_hunter` (NEW)

Отдельный агент поиска клиентов для продаж услуг агентства:

| Правило | Реализация |
|---------|------------|
| Только открытые источники | OpenSERP primary + Google Custom Search API fallback |
| Запрет Telegram/Avito/scrape | `client_hunter_tools.run_google_only_search` |
| УТП на каждого клиента | `ClientUSP` в `ClientHunterResponse.clients[]` |
| Контекст для sales | `client_hunter_context` + `handoff_to_sales` |

Поток: `TaskExecutor` → inject `google_search_results` → LLM готовит УТП →
`handle_client_hunter` → **обязательно `sales`** пишет тексты outreach по USP/handoff.

`lead_hunter` сохранён для сценариев WB/Ozon/Telegram; для монетизации через открытый web используйте `client_hunter`.

### Direction R — Контакты ЛПР + выгрузка писем (NEW)

По факту запуска (см. `AI_Agency - tasks (tasks).json`): у `client_hunter` в карточках
не было контактов ЛПР; `sales` готовил тексты, но отправки нет → нужен export.

| Проблема | Решение |
|----------|---------|
| Нет email/телефона/ЛПР | Поля `decision_maker_role`, `contact_*` в `ClientProspect`; scrape website (`outreach_export.enrich_clients_with_website_contacts`) |
| Нет выгрузки писем | `GET /api/agency/outreach/export?format=md\|json\|csv` + кнопки на дашборде |
| Sales «как будто отправил» | `send_mode=manual_export_only`; qa_feedback: «подготовлено N писем» |

Скачивание артефактов расширено: `client_hunter` (контакты), `sales` (письма).

### Direction Q — Обязательный `sales` после поиска клиентов (NEW)

**Проблема:** PM строил Task Graph с `client_hunter` без `sales` (или клал `sales`
в `excluded_agents`) → лиды/УТП есть, текстов холодных сообщений нет.

**Правило:** при наличии `client_hunter` или `lead_hunter` агент `sales` обязателен
и зависит от hunter-задач (`depends_on`).

| Слой | Изменение |
|------|-----------|
| Код | `task_graph_rules.enforce_sales_after_hunters` после ответа PM |
| PM-промпт | цепочка hunter → sales; запрет exclude sales при поиске клиентов |
| `sales_prompt.txt` | роль Sales (не Analyst); вход `client_hunter_context` + USP/handoff |

Инвариант enforced в коде — даже если LLM-PM ошибётся, sales будет добавлен в граф.

**Тон писем (обновлено):** `sales_prompt.txt` — живой разговорный стиль,
120–220 слов, анти-канцелярит / анти-«ИИ-шаблон»; обязательная подпись:

`С уважением, Иконников Алексей, директор агентства "Деловая экспертиза"`.

Подпись также дописывается в `outreach_export.ensure_sales_signature`, если модель забыла.

### Direction P — ICP-поиск лидов (не SaaS) (NEW)

**Проблема:** эвристика `build_search_queries` строила запросы вида
«клиника медицинский центр + автоматизация/CRM» → Google отдавал обзоры и
вендоров; LLM честно возвращал `clients=[]` и просил «лучшие» запросы, но
поиск уже был выполнен до вызова модели.

**Исправление:**

| Слой | Изменение |
|------|-----------|
| `detect_icp` / шаблоны | Запросы на **сайты бизнесов** (`официальный сайт`, `записаться`, город, ЛПР) |
| Dental/clinic | Legacy-шаблоны «ниша + CRM» отключены (они вредны) |
| Фильтр SERP | `hit_class`: `prospect_candidate` vs `vendor_or_article` |
| 2-й проход | Если prospect-хитов < 2 → `build_refined_queries` по городам |
| Промпты | `client_hunter_prompt.txt` + PM: ICP в task_description, запрет SaaS в clients |
| QA | Чек-лист: нет вендоров в clients, search_queries prospect-oriented |

Пример хороших запросов: `частная стоматология Москва официальный сайт`.

### Direction T — lead_hunter: OpenSERP + анти-галлюцинации + один hunter (NEW)

**Проблема:** `lead_hunter` выдумывал селлеров («Модный Дом», фейковые @telegram /
email), хотя `client_hunter` честно вернул 0. Оба hunter работали параллельно →
`sales` писал письма по галлюцинациям.

| Исправление | Где |
|-------------|-----|
| OpenSERP inject до LLM | `task_executor._inject_lead_search` + `lead_hunter_tools` |
| Промпт | только `google_search_results`; запрет выдуманных контактов |
| Post-filter | `filter_hallucinated_leads` — лид без URL из SERP отбрасывается |
| Маршрутизация | `prefer_single_hunter`: ровно один из client_hunter / lead_hunter |
| Sales | письма только по компаниям из подтверждённого контекста |

Селлерский goal → `lead_hunter`; клиники/общий ICP → `client_hunter`.

### Direction Y — Устойчивость NocoDB (NEW)

**Проблема:** при недоступности NocoDB агенты «терялись» — статусы/задачи не
обновлялись, поведение оркестратора становилось непредсказуемым (один сбой
чтения/записи без повтора).

**Решение:** единый `nocodb_request()` в `nocodb.py`:

1. Таймаут — константа `Config.NOCODB_TIMEOUT_SEC` (default **60** с).
2. При временной ошибке — до **3 повторов** с паузами **10 → 30 → 60** с
   (`NOCODB_MAX_ATTEMPTS=4`, `NOCODB_RETRY_DELAYS_SEC=10,30,60`).
3. Retryable: connection/timeout, HTTP 429/5xx; 4xx — сразу без sleep.

Тесты: `tests/test_nocodb.py` → `TestNocodbRequestRetry`.

### Direction X — Итерации проекта после завершения (NEW)

**Задача:** после `completed` человек даёт замечания → PM анализирует → доработка.
Нужно и для текущего проекта, и для любого из истории.

**Выбранный вариант: B — итерация на том же `project_id`** (не новый проект).

| Почему не A (новый проект) | Почему B |
|----------------------------|----------|
| Дублирует goal/клиента, рвёт артефакты/outreach по Id | Сохраняет history, export, один бизнес-проект |
| Сложнее resume/история | Близко к human-review; PM-replan добавляет `iterN_*` задачи |

**Поток:**

1. UI: «Новая итерация» в панели управления (текущий/открытый проект);
   в истории — кнопка «Доработать» в каждой строке списка и в баннере просмотра.
2. `POST /api/agency/refine` `{ human_prompt, project_id?, resume }`
3. `initialize(project_id)` → `start_project_iteration()`:
   - архив `final_report` в `metrics.iteration_history[]`
   - `metrics.iteration += 1` (опц. поле `iteration` в NocoDB)
   - PM (`pm_task_graph`): только задачи доработки; `task_id` = `iterN_…`
   - `depends_on` только внутри нового графа; опора на прошлое — `previous_task_ids` в `input_data`
4. `status=in_progress`, запуск `run()`; старые completed-задачи остаются.

Файлы: `project_iteration.py`, `orchestrator.start_project_iteration`,
`/api/agency/refine`, дашборд (`btn-refine`, история).

### Direction W — Документация интеграции (tech_writer) (NEW)

**Проблема:** tech_writer мог выдать user_guide/КП без обязательного описания
интеграции. Для сопрововления n8n-сценария нужны технические разделы, а не
маркетинговый текст.

**Обязательный документ:** `integration_guide` или `tech_guide` с минимумом:

| # | Раздел | Зачем |
|---|--------|--------|
| 1 | Цель интеграции | Задача сценария |
| 2 | Источник и получатель | Откуда/куда данные, где искать сбой |
| 3 | Версия API | Диагностика совместимости |
| 4 | Webhook / poll | Способ получения событий и почему |
| 5 | Пагинация | Обход страниц, если есть |
| 6 | Лимиты API | Как учтены в сценарии |
| 7 | Контракт данных | Поля, формат, условия стопа |
| 8 | Критичные поля | Нельзя ломать |
| 9 | Обработка ошибок | Коды, retry, алерты, журнал |
| 10 | Адаптер | Слой нормализации |
| 11 | Тестирование | Данные и сценарии устойчивости |
| 12 | Сопровождение | Ответственный, поддержка |

**Ключевое (без этого QA/схема не примут):** критичные поля, контракт данных,
обработка ошибок.

Изменения: `tech_writer_prompt.txt`, `Document.type=integration_guide`,
валидатор `TechWriterResponse`, чек-лист QA Gate.

### Direction V — Один workflow на декомпозицию developer (NEW)

**Проблема (факт из `AI_Agency - tasks.json`):** цель = один устойчивый сценарий
(источник → обработка → внешний API + retry/ошибки/журнал). PM разрезал на
`dev_002…dev_005` (loop, backoff, классификация ошибок, статус). Каждая задача
получила **полный** `workflow_blueprint` и контракт developer «сериализуй весь
blueprint в n8n JSON» → 4 почти одинаковых workflow под разными именами.

**Корневая причина:** декомпозировали *работу*, но не артефакт: не было
`artifact_mode`, blueprint шёл в каждую `dev_*`, merge отсутствовал.

**Исправление:**

| Слой | Изменение |
|------|-----------|
| `core/dev_decomposition.py` | Нормализация: при blueprint → ≤1 `full_workflow`; фиче-срезы склеиваются |
| `handle_architect` | Полный blueprint только у `full_workflow`; prep/spec без `n8n_json` |
| `pm_prompt` + decompose prompt | Запрет резать retry/ошибки/журнал на отдельные n8n-задачи |
| `developer_prompt` | Ветвление по `artifact_mode` |
| `task_executor` | n8n-валидация только для `full_workflow`; strip у prep/spec |

Допустимо: `prep` (таблица/env) + одна `full_workflow`. Несколько `full_workflow` —
если architect вернул `workflow_blueprints[]` (Direction AE): **по одной задаче на
каждый** независимый сценарий. Склеивать два сценария в один n8n_json запрещено.

### Direction U — Мульти-провайдерный LLM (NEW)

**Проблема:** все агенты ходили только в Yandex Responses API через `utils.call_llm`.
Нельзя было выбрать OpenAI / Grok / Anthropic / DeepSeek / GigaChat с дашборда.

**Решение:**

| Слой | Изменение |
|------|-----------|
| `core/llm_engine.py` | Реестр 6 провайдеров, активный в памяти процесса, единый `invoke()` |
| `utils.call_llm` | Тонкая обёртка → `llm_engine.invoke` (контракт агентов без изменений) |
| API | `GET /api/agency/llm/providers`, `POST /api/agency/llm/provider`, поле `llm` в `/status` |
| Дашборд | Селектор «Нейросеть» в `.controls`; смена без рестарта; блокировка во время `running` |
| `.env` | `LLM_PROVIDER` + ключи `OPENAI_*` / `GROK_*` / `ANTHROPIC_*` / `DEEPSEEK_*` / `LLM_*` / `GIGACHAT_*` |

Стили API: OpenAI/Grok/DeepSeek — `chat/completions`; YandexGPT — Responses; Anthropic — Messages; GigaChat — OAuth + chat completions.

Тесты: `tests/test_llm.py`, `tests/test_llm_engine.py`.

#### Кэширование промптов OpenAI (не забывать)

Модели **GPT-4o и новее** поддерживают prompt caching. В **базовом (автоматическом)
режиме код менять не нужно** — кэш прозрачен для всех запросов через
`llm_engine._call_openai_chat`.

| Правило | Суть |
|---------|------|
| Мин. длина | Кэшируются запросы от **1024 токенов** |
| Префикс | Совпадение начала запроса должно быть **точным**; любое отличие сбрасывает кэш |
| Что входит | messages, изображения, tools, structured-output схемы |
| Авто-режим | OpenAI сам ставит точку кэша на конец подходящего префикса (default) |
| Ручной режим | Явные cache breakpoints — опционально позже, не нужен для старта |

**Как у нас:** в `invoke` сначала стабильный `system` (промпт агента + schema),
затем переменный `user` (задача). Длинный неизменный system (≥1024 tok) даёт
скидку и меньшую latency на повторных вызовах того же агента. Не вставлять в
начало system таймстемпы/UUID — иначе префикс разъедется.

Ручной режим и учёт `cached_tokens` в usage — возможное усиление (U+); пока
достаточно автоматического режима.

**Параметр лимита ответа (GPT-5 / ChatGPT-5.x):** модели вроде `gpt-5*`,
`chatgpt5*` / `chatgpt-5*` не принимают `max_tokens` — только
`max_completion_tokens`. В `llm_engine._call_openai_chat` для OpenAI это
выбирается по имени модели; при 400 «Use max_completion_tokens» — автоматический
retry. Grok/DeepSeek/GigaChat по-прежнему шлют `max_tokens`.

### Direction S — Тихое логирование (NEW)

**Проблема:** poll дашборда (`GET /status` каждые 2.5 с) заливал консоль:
uvicorn access, `Main` timing, NocoDB «получено N задач» + полный URL
`find_project_by_status`, плюс **дубль**: `_attach_resume_flags` снова искал
resumable-проект в БД на каждый poll, хотя `current_project` уже в памяти.

| Исправление | Где |
|-------------|-----|
| `core/logging_setup.py` | `LOG_LEVEL`, `NOCODB_LOG_LEVEL` (default WARNING), фильтр uvicorn `/status` |
| Middleware | `/api/agency/status` и `/static/*` → DEBUG |
| `_attach_resume_flags` | без NocoDB, если статус уже resumable; `known_resumable=` без второго поиска |
| NocoDB read/PATCH | INFO → DEBUG для рутины |

В INFO остаются старт/стоп оркестратора, LLM, ошибки, создание задач/проектов,
результаты поиска OpenSERP.

### Direction L — Замечания по эксплуатации (ИСПРАВЛЕНО)

Три замечания по факту использования дашборда/оркестратора:

**1. Дашборд не отражал картину в реальном времени.**
Был фиксированный `setInterval(refreshData, 10000)` (10 с) независимо от того,
идёт ли выполнение. Заменено на адаптивный self-scheduling poll-loop
(`index.html`): пока агентство `running` — опрос каждые **2.5 с**
(`POLL_MS_RUNNING`), в простое — раз в **10 с** (`POLL_MS_IDLE`). Подсказка
`#poll-hint` показывает актуальный интервал. Это не полный realtime (WebSocket
остаётся в Direction H ниже), но прогресс задач/токенов теперь виден почти
сразу, а не с задержкой до 10 секунд.

**2. Кнопка «Продолжить» и статусы проектов (обновлено).**

| Ситуация | Поведение |
|----------|-----------|
| В памяти `None`, в БД есть resumable | `/status` показывает проект; `can_resume=true` |
| Текущий проект `completed` | Кнопка **активна**; resume ищет другой проект |
| Resume | Ищет `in_progress → stopped → needs_human_review` (не создаёт новый) |
| Найден `stopped` / `in_progress` | Загрузка + запуск цикла |
| Найден `needs_human_review` | Загрузка, цикл **не** стартует → ожидание Human Review |
| Кандидатов нет | `404`, подсказка создать новый проект |

Флаги в `/status`: `can_resume`, `resume_allowed`, `resumable_project_id`, `resumable_status`.

**3. Developer: лимит итераций по умолчанию поднят до 6.**
Сложные n8n-интеграции чаще требуют доработки по фидбеку QA, чем 3 попытки.
Добавлен `Config.DEVELOPER_MAX_ITERATIONS` (env `DEVELOPER_MAX_ITERATIONS`,
по умолчанию `6`), применяется:
- `agent_handlers.py` → `handle_architect()`: подзадачи `dev_*` создаются с
  `max_iterations: Config.DEVELOPER_MAX_ITERATIONS` (было хардкод `3`).
- `task_executor.py` → `TaskExecutor.execute()`: фолбэк, если у задачи `agent_name
  == "developer"` не задано `max_iterations` в БД — используется тот же дефолт
  (было общее `MAX_TASK_ITERATIONS = 3` для всех агентов).
- Промпт PM (`orchestrator.py` → `_create_initial_task_graph`) явно указывает:
  «3 по умолчанию, но для `developer` — 6».
Остальные агенты (`analyst`, `architect`, `qa`, …) не затронуты — у них
`MAX_TASK_ITERATIONS = 3`, как раньше.

### Direction M — OpenSERP: бесплатный поиск клиентов (NEW)

[OpenSERP](https://github.com/karust/openserp) — self-hosted open-source SERP API
(Google/Yandex/Bing/DuckDuckGo/Baidu/Ecosia), не требующий платных ключей.
Интегрирован как **основной** источник поиска для `client_hunter`, заменяя
платный Google Custom Search API в качестве primary-провайдера (Google API
остаётся резервным сценарием — чек-лист устойчивости, п.5).

**Почему именно OpenSERP, а не смена политики «только Google»:**
Дедицированный `/google/search` эндпоинт OpenSERP бьёт по тому же Google —
это тот же канал, что и раньше, просто без платного API-ключа и его лимитов.
Политика client_hunter_prompt.txt («единственный канал — Google», `source:
"google"`) не нарушается.

| Слой | Что сделано |
|------|-------------|
| Клиент | `core/openserp_client.py` → `OpenSerpClient.search()` — HTTP GET `{base_url}/{engine}/search`, парсит `results[]`, фильтрует не-organic (реклама/related) |
| Конфиг | `OPENSERP_*` + **`openserp/config.yaml`**: `app.timeout: 120` (дефолт upstream 15 с → 504 на Google) |
| Клиент | `openserp_client.py`: retry на 504/timeout + `/mega/search?mode=any` + fallback engines |
| Резерв | `core/client_hunter_tools.py` → `run_google_only_search()`: OpenSERP пуст/недоступен для запроса → пробуем Google Custom Search API (если настроен); оба пусты → честный `[]` |
| Промпт | `client_hunter_prompt.txt`: `google_search_results` теперь описан как «через OpenSERP или резервно Google Custom Search API» |
| Устойчивость | Любая ошибка сети/HTTP/JSON от OpenSERP — временная, ловится внутри `OpenSerpClient.search()`, никогда не бросает исключение наружу |

Запуск OpenSERP локально (**с agency-конфигом**, иначе дефолт `timeout: 15` → 504):

```bash
# Linux/macOS
docker run --rm -p 127.0.0.1:7000:7000 \
  -v "$PWD/openserp/config.yaml:/config.yaml:ro" \
  karust/openserp:latest serve --config /config.yaml

# Windows (PowerShell, из www/ai_agency)
docker run --rm -p 127.0.0.1:7000:7000 `
  -v "${PWD}/openserp/config.yaml:/config.yaml:ro" `
  karust/openserp:latest serve --config /config.yaml
```

Приоритет источников на каждый search-запрос (не на всю пачку запросов —
если по одному query OpenSERP пуст, а по другому ответил, второй Google-запрос
не делается зря):

```
для каждого query из search_queries:
    hits = OpenSERP.search(query)          # основной, бесплатный
    если hits пуст И настроен GOOGLE_API_KEY/GOOGLE_CX:
        hits = GoogleCustomSearchAPI.search(query)   # резерв
    collect(hits)
```

Тесты: `tests/test_openserp.py` (retry 504, mega/fallback) + `tests/test_client_hunter.py`
(primary/fallback/оба пусты) — 24 passed.

### Направление H. UI на React + WebSocket (ТЗ №5)

| Подэтап | Статус | Содержание |
|---------|--------|------------|
| **2.1 WebSocket** | **РЕАЛИЗОВАНО** | `GET/WS /api/agency/ws` — push того же payload, что `GET /status`; vanilla `index.html` подключается к WS, HTTP-poll — fallback |
| 2.2 React (Vite) | следующий | Task Graph, метрики, Retry failed, syntax highlight |
| 2.3 | после 2.2 | полный отказ от poll при стабильном WS |

**Фаза 2.1 (сейчас):**

| Компонент | Роль |
|-----------|------|
| `core/agency_ws.py` | `StatusHub`, push-loop, ping/pong, `schedule_broadcast` |
| `main.py` → `build_agency_status()` | общий снимок для REST и WS |
| `main.py` → `@app.websocket("/api/agency/ws")` | snapshot при connect + подписка |
| `index.html` | `connectStatusWs()`; при обрыве — poll 2.5/10 с |
| Env | `WS_PUSH_MS_RUNNING` (default 1500), `WS_PUSH_MS_IDLE` (10000) |

Немедленный push после start/stop/resume/refine/human-review/LLM switch/increase-tokens.

Тесты: `tests/test_agency_ws.py`.

### Roadmap — Фаза 1 (качество n8n) — РЕАЛИЗОВАНО (Direction Z)

Direction K в heuristic + smoke schemaDelta + CI Layer C (`N8N_VALIDATOR_OFFICIAL=on`, Node ≥22).

### Направление G. Параллельное выполнение задач (не реализовано)

Текущий цикл выполняет задачи последовательно. Независимые задачи могут выполняться параллельно через `asyncio` / `ThreadPoolExecutor` внутри Orchestrator.

### Направление H. Продуктовая готовность

- Авторизация API (JWT / API key)
- Rate limiting для LLM-вызовов
- Мониторинг токенов в реальном времени
- Webhook-уведомления о завершении задач

### Направление F (Тесты, надёжность, безопасность) — РЕАЛИЗОВАНО в Direction F

Внимание: Direction F в список открытых вопросов перемещена в **реализованные** (см. выше). Остаются:

### Направление I. Масштабирование blueprint для сложных workflow (будущее)

Если workflow станут регулярно превышать 5–7 нод с множественным ветвлением и несколькими интеграциями, отдельный агент «workflow designer» станет оправданным для выделения шага blueprint-инженера из архитектора.

---

## 15. Быстрые ссылки

| Компонент | Файл | Примечание |
|-----------|------|------------|
| Главный цикл | `orchestrator.py` → `run()` | |
| Task Graph | `orchestrator.py` → `_create_initial_task_graph()` | |
| Deadlock resolution | `orchestrator.py` → `resolve_deadlock()` | |
| Выполнение задачи | `task_executor.py` → `TaskExecutor.execute()` | |
| QA Gate | `qa_gate.py` → `QAGate.run()` | |
| Спец-хендлеры | `agent_handlers.py` → `AgentHandlers` | |
| Pydantic-модели | `schemas.py` → все классы + `AGENT_MODELS` | |
| Единый парсинг LLM | `schemas.py` → `call_and_parse_llm()` | |
| Мульти-LLM | `llm_engine.py` + `/api/agency/llm/*` | OpenAI, Grok, Anthropic, DeepSeek, YandexGPT, GigaChat |
| Dev decomposition | `dev_decomposition.py` | ≤1 full_workflow на blueprint (Direction V) |
| Итерации проекта | `project_iteration.py` + `/api/agency/refine` | замечания → PM-replan на том же Id (Direction X) |
| NocoDB клиенты | `nocodb.py` → 3 класса + `nocodb_request` | timeout/retry (Direction Y) |
| NocoDB прокси | `main.py` → `nocodb_proxy()` | Защищён whitelist |
| Промпты | `prompts/*.txt` | 9 файлов |
| Конфигурация | `config.py` → `Config` | `.env` |
| Поиск клиентов | `openserp_client.py` + `client_hunter_tools.py` | OpenSERP primary, Google API fallback |
| Task Graph rules | `task_graph_rules.py` | sales обязателен после client_hunter/lead_hunter |
| Outreach export | `outreach_export.py` + `/api/agency/outreach/export` | контакты ЛПР + выгрузка писем |
| Status WebSocket | `agency_ws.py` + `/api/agency/ws` | Фаза 2.1 push (poll = fallback) |
| Agent Context | `agent_context.py` + `agency_agent_context_mcp.py` | Direction AG память итераций |
| n8n-validator | `n8n_validator.py` + `validate-n8n.js` + official engine | A heuristic+K → B local → C official + smoke (Direction Z) |

---

**Последнее обновление:** Jul 30, 2026. Direction AJ: parent developer completed после сабтасков (iterN_dev_*).
