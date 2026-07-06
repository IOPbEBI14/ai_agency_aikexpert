# AI Agency OS — Архитектура и описание проекта

**Дата обновления:** Jul 7, 2026 | **Версия:** 2.1 | **Язык:** Python 3.10+

> Документ отражает состояние после рефакторинга Directions A–F (Sprints 0–1).
> Все 94 теста проходят. Ключевое изменение: встроен node-level workflow blueprint в архитектора для закрытия разрыва между концептуальным проектированием и конкретной сериализацией n8n JSON.

---

## 1. Назначение проекта

**AI Agency OS** — оркестратор из 9 специализированных ИИ-агентов (паттерн Supervisor-Workers) для автоматизации e-commerce (селлеры Wildberries/Ozon).

**Основной сценарий:**
1. **PM** (Project Manager) анализирует цель проекта и строит Task Graph с зависимостями
2. **Рабочие агенты** выполняют задачи параллельно или последовательно по графу
3. **QA Gate** проверяет каждый результат (до 3 итераций на задачу)
4. **PM** финализирует проект, формируя markdown-отчёт для клиента

**Инфраструктура:**
- **Хранение состояния:** NocoDB (3 таблицы: projects, tasks, agent_logs) — stateless, допускает перезапуск
- **LLM:** Yandex AI Studio (Responses API, модель YandexGPT)
- **Frontend:** Vanilla HTML/JS дашборд + marked.js для рендеринга отчётов
- **Framework:** Flask + CORS, однопоточный HTTP + `threading.Thread` для оркестрации

---

## 2. Структура репозитория

```
ai_agency_aikexpert/
├── ANALYSIS.md                  # ← этот файл
├── README.md
├── .cursorrules                 # Python best practices (LlamaFarm-style)
├── .skills/                     # Skill-файлы (patterns.md, async.md, typing.md…)
└── www/ai_agency/
    ├── main.py                  # Flask API (тонкий HTTP-слой + nocodb_proxy)
    ├── index.html               # Дашборд мониторинга
    ├── pytest.ini
    ├── core/
    │   ├── orchestrator.py      # ⭐ Главный цикл, task graph, deadlock, финализация
    │   ├── task_executor.py     # ★ [NEW] Выполнение одной задачи (промпт → LLM → QA)
    │   ├── qa_gate.py           # ★ [NEW] QA-проверка результатов агентов
    │   ├── agent_handlers.py    # ★ [NEW] Спец-хендлеры для 4 агентов
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
| **main.py** | **~310** | Flask API, `nocodb_proxy` (защищённый) | 🟠 ВЫСОКИЙ |
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
│  • Real-time статус задач и агентов                         │
│  • Markdown-рендеринг финального отчёта                     │
└────────────────────┬────────────────────────────────────────┘
                     │ HTTP (polling)
                     ▼
       ┌──────────────────────────────┐
       │      Flask API (main.py)     │
       │  GET  /api/agency/status     │
       │  POST /api/agency/start      │
       │  POST /api/agency/stop       │
       │  POST /api/agency/resume     │
       │  POST /api/agency/human-review│
       │  GET/POST/PATCH /api/nocodb  │  ← защищённый прокси
       └────────────┬─────────────────┘
                    │ threading.Thread
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
initialize()
  ├─ Найти in_progress/stopped проект → загрузить
  └─ Не найден → create_project() + _create_initial_task_graph()

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
architect    →  QA → PM декомпозиция → dev_001…dev_N
                     ↓
developer    →  QA Gate (output_data передаётся в qa через dependency_outputs)
                     ↓
qa           →  completed
                     ↓
tech_writer  →  QA Gate
```

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

---

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

### Направление G. Параллельное выполнение задач (не реализовано)

Текущий цикл выполняет задачи последовательно. Независимые задачи (с пустым `depends_on` или совпадающими зависимостями) могут выполняться параллельно через `asyncio` или `ThreadPoolExecutor`.

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

---

**Последнее обновление:** Jul 7, 2026. Directions A–F реализованы. 94/94 теста ✅.
