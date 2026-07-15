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
    ├─ Проверка бюджета токенов → needs_human_review если > 80%
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
lead_hunter →  leads_context + leads_handoff_to_sales
                     ↓
sales        →  sales_context
                     ↓
analyst      →  analyst_context + handoff_to_architect
                     ↓
architect    →  QA Gate → PM декомпозиция → dev_001…dev_N
                     ↓
developer    →  QA Gate (каждая dev_*; output_data → qa через dependency_outputs)
                     ↓
qa           →  completed
                     ↓
tech_writer  →  QA Gate
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
| `tech_writer_prompt.txt` | ✅ | ✅ `TechWriterResponse` | `commercial_proposal` добавлен в `Document.type` |
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
---

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
| Только открытые источники | Google Custom Search API (`GOOGLE_API_KEY`, `GOOGLE_CX`) |
| Запрет Telegram/Avito/scrape | `client_hunter_tools.run_google_only_search` |
| УТП на каждого клиента | `ClientUSP` в `ClientHunterResponse.clients[]` |
| Контекст для sales | `client_hunter_context` + `handoff_to_sales` |

Поток: `TaskExecutor` → inject `google_search_results` → LLM готовит УТП → `handle_client_hunter` сохраняет контекст.

`lead_hunter` сохранён для сценариев WB/Ozon/Telegram; для монетизации через открытый web используйте `client_hunter`.

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

**2. Проект в статусе `stopped` был недоступен для «Продолжить».**
Причина: `GET /api/agency/status` брал статус только из `orchestrator.current_project`
(объект в памяти процесса). Если процесс перезапускался или `current_project`
был перезаписан созданием другого проекта — в памяти `None`, эндпоинт возвращал
`status: "idle"`, и кнопка «Продолжить» блокировалась по условию
`!['stopped', 'needs_human_review'].includes(status)`, хотя в БД лежал вполне
резюмируемый проект. Исправление в `main.py`: добавлена `_find_resumable_project()`
— при отсутствии `current_project` в памяти статус ищется в NocoDB по приоритету
`in_progress → stopped → needs_human_review` (тот же порядок, что и в
`Orchestrator.initialize()`) и возвращается как текущий live-статус. Сам `POST
/api/agency/resume` уже был корректен (вызывает `initialize()` без аргументов) —
проблема была только в отображении состояния кнопки.

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
| Конфиг | `Config.OPENSERP_BASE_URL` (по умолчанию `http://localhost:7000`), `Config.OPENSERP_ENGINE` (по умолчанию `google`), `Config.OPENSERP_TIMEOUT_SEC` |
| Резерв | `core/client_hunter_tools.py` → `run_google_only_search()`: OpenSERP пуст/недоступен для запроса → пробуем Google Custom Search API (если настроен); оба пусты → честный `[]` |
| Промпт | `client_hunter_prompt.txt`: `google_search_results` теперь описан как «через OpenSERP или резервно Google Custom Search API» |
| Устойчивость | Любая ошибка сети/HTTP/JSON от OpenSERP — временная, ловится внутри `OpenSerpClient.search()`, никогда не бросает исключение наружу |

Запуск OpenSERP локально:

```bash
docker run --rm -p 127.0.0.1:7000:7000 karust/openserp:latest serve -a 0.0.0.0 -p 7000
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

Тесты: `tests/test_openserp.py` (11 тестов — парсинг, фильтр non-organic, timeout/connection
error → `[]`, clamp лимита 1–100) + обновлённые `tests/test_client_hunter.py`
(primary/fallback/оба пусты).

### Направление H. UI на React + WebSocket (ТЗ №5) — СЛЕДУЮЩИЙ ЭТАП

После стабилизации FastAPI:
1. WebSocket `/ws` в FastAPI (push статусов задач)
2. React (Vite) дашборд: Task Graph, метрики токенов, Retry failed, syntax highlight
3. Замена adaptive polling (2.5–10 с, см. Direction L) на настоящий push realtime

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
| NocoDB клиенты | `nocodb.py` → 3 класса | |
| NocoDB прокси | `main.py` → `nocodb_proxy()` | Защищён whitelist |
| Промпты | `prompts/*.txt` | 9 файлов |
| Конфигурация | `config.py` → `Config` | `.env` |
| Поиск клиентов | `openserp_client.py` + `client_hunter_tools.py` | OpenSERP primary, Google API fallback |

---

**Последнее обновление:** Jul 15, 2026. OpenSERP — бесплатный поиск для `client_hunter` (Direction M).
