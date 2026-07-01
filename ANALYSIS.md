# AI Agency OS — Структурированный анализ проекта

**Дата анализа:** Jul 1, 2026 | **Версия проекта:** 1.0 | **Язык:** Python 3.10+

---

## 1. Назначение проекта

**AI Agency OS** — оркестратор из 9 специализированных ИИ-агентов (паттерн Supervisor-Workers) для автоматизации e-commerce (селлеры Wildberries/Ozon). 

**Основной сценарий:**
1. PM (Project Manager) анализирует цель проекта и строит Task Graph
2. Рабочие агенты (lead_hunter, sales, analyst, architect, developer, crm_customizer, qa, tech_writer) выполняют задачи последовательно
3. QA-агент проверяет каждый результат (до 3 итераций)
4. PM финализирует проект, формируя markdown-отчёт для клиента

**Состояние:** Хранится в NocoDB (stateless архитектура, допускает перезапуск).  
**LLM:** Yandex AI Studio (Responses API).  
**Frontend:** Vanilla HTML/JS дашборд + marked.js для рендеринга.

---

## 2. Структура репозитория

```
ai_agency_aikexpert/
├── README.md                    # Краткое описание
├── .cursorrules                 # Python best practices (LlamaFarm-style)
├── .skills/                     # Skill-файлы (patterns.md, async.md, typing.md…)
├── package-lock.json            # Пустой npm lockfile
└── www/ai_agency/               # Основное приложение
    ├── main.py                  # Flask API (тонкий HTTP-слой)
    ├── index.html               # Дашборд мониторинга
    ├── core/
    │   ├── orchestrator.py      # ⭐ Вся бизнес-логика оркестрации
    │   ├── schemas.py           # ⭐ Pydantic-модели для всех агентов
    │   ├── nocodb.py            # Клиенты NocoDB (3 таблицы)
    │   ├── utils.py             # LLM, промпты, логирование
    │   ├── config.py            # Настройки из .env
    │   ├── lead_tools.py        # Реальный поиск лидов (скрапинг/API)
    │   ├── prompt_builder.py    # Генератор JSON Schema для промптов
    │   └── llm_parser.py        # Альтернативный парсер LLM
    ├── prompts/                 # 9 системных промптов (.txt)
    │   ├── pm_prompt.txt
    │   ├── lead_hunter_prompt.txt
    │   ├── sales_prompt.txt
    │   ├── analyst_prompt.txt
    │   ├── architect_prompt.txt
    │   ├── developer_prompt.txt
    │   ├── crm_customizer_prompt.txt
    │   ├── qa_prompt.txt
    │   └── tech_writer_prompt.txt
    └── tests/
        ├── conftest.py          # Pytest fixtures
        ├── test_orchestrator.py # Тесты оркестратора
        ├── test_schemas.py      # Тесты Pydantic-моделей
        ├── test_llm.py          # Тесты call_llm
        ├── test_qa.py           # Тесты QA Gate
        └── pytest.ini
```

---

## 3. Карта модулей по важности и размеру

| Модуль | Строк | Роль | Приоритет |
|--------|-------|------|-----------|
| **orchestrator.py** | **1657** | Цикл выполнения, task graph, QA gate, декомпозиция, обработка ошибок | 🔴 КРИТИЧНЫЙ |
| **schemas.py** | 597 | Pydantic-модели всех агентов + парсинг JSON | 🔴 КРИТИЧНЫЙ |
| **nocodb.py** | 454 | 3 клиента для работы с таблицами NocoDB | 🟠 ВЫСОКИЙ |
| **main.py** | 277 | Flask API, threading, NocoDB-прокси | 🟠 ВЫСОКИЙ |
| **utils.py** | 243 | LLM API wrapper, загрузка промптов, логирование | 🟠 ВЫСОКИЙ |
| **index.html** | 1037 | Дашборд (vanilla JS, real-time updates) | 🟡 СРЕДНИЙ |
| **lead_tools.py** | 308 | Скрапинг/API для поиска лидов (WB, Ozon, TG, Google, Avito) | 🟡 СРЕДНИЙ |
| **prompts/*.txt** | 147–317 | Системные промпты для 9 агентов | 🟡 СРЕДНИЙ |
| **llm_parser.py** | 130 | Дубль парсинга LLM (дублирует логику) | 🟡 РЕФАКТОРИНГ |
| **prompt_builder.py** | 64 | Дубль сборки промпта (дублирует логику) | 🟡 РЕФАКТОРИНГ |
| **config.py** | 41 | Загрузка .env переменных | 🟢 НИЗКИЙ |
| **tests/** | 116–613 | pytest (15+ классов тестов) | 🟠 ВЫСОКИЙ |

---

## 4. Архитектура

### Диаграмма потока

```
┌─────────────────────────────────────────────────────────────┐
│                     Dashboard (index.html)                  │
│  • Real-time status                                         │
│  • Task cards with results                                  │
│  • Final report markdown                                    │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
          ┌─────────────────────────┐
          │   Flask API (main.py)   │
          │ • /api/agency/start     │
          │ • /api/agency/status    │
          │ • /api/nocodb/* (proxy) │
          └────────┬────────────────┘
                   │
          ┌────────▼────────┐
          │  Orchestrator   │
          │  (threading)    │
          └────────┬────────┘
                   │
        ┌──────────┼──────────┐
        │          │          │
        ▼          ▼          ▼
    ┌────────┐ ┌─────────┐ ┌──────────┐
    │Pydantic│ │LLM API  │ │ NocoDB   │
    │Schemas │ │(Yandex) │ │(REST v3) │
    └────────┘ └─────────┘ └──────────┘
```

### Модель данных (NocoDB)

**Таблица `projects`**
- `project_name`, `client_name`, `goal` — описание
- `status` — in_progress | completed | stopped | needs_human_review
- `tokens_used`, `token_budget` — бюджет
- `final_report` — markdown отчёт (LongText)
- `metrics` — JSON со статистикой

**Таблица `tasks`**
- `task_id` — уникальный ID (task_001, dev_001…)
- `project_id` — связь с проектом
- `agent_name` — кто выполняет
- `task_description`, `input_data`, `output_data` — handoff
- `status` — pending | in_progress | completed | failed
- `depends_on` — JSON-массив ID зависимостей
- `qa_approved`, `qa_feedback` — результаты QA
- `iteration_count`, `max_iterations` — retry logic

**Таблица `agent_logs`**
- `agent_name`, `project_id`, `status`
- `task_description` — краткое описание
- `full_response` — JSON ответа от агента
- `tokens_used`, `timestamp` — метрики

---

## 5. Главный цикл оркестрации

**Файл:** `orchestrator.py`, метод `run()` (строки ~405–536).

### Последовательность

1. **Инициализация** (`initialize`)
   - Загружает in_progress/stopped проект или создаёт новый

2. **Task Graph** (`_create_initial_task_graph`)
   - PM вызывается с Pydantic-валидацией (`PMTaskGraph`)
   - JSON Schema добавляется в промпт
   - Задачи сохраняются в NocoDB

3. **Цикл выполнения** (ГЛАВНОЕ)
   ```python
   while agency_running and iteration < MAX_TOTAL_ITERATIONS:
       # Проверка бюджета токенов
       # Проверка completed parent tasks
       # Анализ статуса: completed | pending | in_progress | failed
       # Если все completed → финализация
       # Поиск готовых задач (depends_on совпадают с completed)
       # execute_task() → run_qa_gate() → обновление статуса
   ```

4. **Обработка тупиков** (`resolve_deadlock`)
   - Если есть pending задачи, но ни одна не готова
   - PM предлагает: update_dependencies | skip_tasks | create_tasks | stop_project

5. **Финализация** (`finalize`)
   - PM собирает результаты, формирует markdown-отчёт (`PMFinalReport`)

### Обработка застрявших задач

Если задача в `in_progress` > 5 минут — возвращается в `pending` (строки ~490–511).

---

## 6. QA Gate

**Файл:** `orchestrator.py`, метод `run_qa_gate()` (строки ~1042–1210).

### Поток

1. Задача выполнена агентом → сохранена в `output_data`
2. QA вызывается с Pydantic `QAResponse` (парсинг через `call_and_parse_llm`)
3. QA проверяет результат, возвращает:
   - `summary`, `tests_total/passed/failed`
   - `issues` — список проблем (severity: critical|high|medium|low)
   - `warnings`, `recommendations`
4. **Approved**, если `tests_failed == 0` AND нет critical/high issues
5. Если **не approved**:
   - Увеличивается `iteration_count`
   - Если `iteration_count < max_iterations` → возврат в `pending` с `qa_feedback`
   - Иначе → `status = failed`

---

## 7. Спецобработчики агентов

**Файл:** `orchestrator.py`.

### `_handle_architect` (строки ~825–982)
- Architect проходит QA
- После QA → PM декомпозирует на подзадачи для developer (`PMDecomposition`)
- Создаёт `dev_001, dev_002…` с зависимостями
- Помечает architect как completed

### `_handle_lead_hunter_with_tools` (строки ~1475–1620)
- Реальный поиск через API/скрапинг (`lead_tools`)
- Сохраняет результаты в `current_project["leads_context"]`
- Помечает как completed (обходит QA)

### `_handle_sales` (строки ~1623–1670)
- Извлекает сообщения и квалификацию
- Сохраняет в `current_project["sales_context"]`
- Помечает как completed

### `_handle_analyst` (строки ~1673–1726)
- ROI расчёты, болевые точки, КП структура
- Сохраняет в `current_project["analyst_context"]`
- Помечает как completed

### Остальные агенты
- lead_hunter, sales, analyst → custom handling (не идут через обычный QA Gate)
- Остальные (developer, crm_customizer, tech_writer) → обычный QA Gate

---

## 8. Выявленные проблемы и области улучшения

### 🔴 КРИТИЧНЫЕ

1. **`orchestrator.py` перегружен (1657 строк)**
   - Содержит: цикл, QA, per-agent хендлеры, декомпозицию, deadlock resolution
   - **Решение:** Выделить в отдельные классы (`QAGate`, `TaskExecutor`, `AgentHandlers`)

2. **Тройное дублирование парсинга LLM**
   - `orchestrator.call_agent_with_validation()` (строки ~125–220)
   - `schemas.call_and_parse_llm()` (строки ~600–680)
   - `llm_parser.parse_llm_response()` (строки ~16–101)
   - **Решение:** Оставить одну реализацию в `schemas.py` или `utils.py`, остальное удалить

3. **Дубль `build_prompt_with_schema`**
   - В `orchestrator.py` (метод)
   - В `prompt_builder.py` (функция)
   - **Решение:** Консолидировать в `prompt_builder.py`

4. **Дубль `_qa_response_to_result`**
   - Одна реализация строки ~986–1042, вторая ~1227–1277
   - **Решение:** Удалить вторую копию

### 🟠 ВЫСОКИЙ ПРИОРИТЕТ

5. **Lead Hunter активен только Telegram**
   - WB/Ozon/Google/Avito закомментированы (строки ~1513–1580)
   - **Причина:** Вероятно, ошибки при тестировании
   - **Решение:** Задокументировать причину или вернуть функциональность

6. **Рассинхронизация конфигурации**
   - `config.py`: `TOKEN_BUDGET = int(os.getenv("TOKEN_BUDGET", 30000))`
   - `readme.md`: упоминает 500,000 токенов
   - **Решение:** Синхронизировать defaults

7. **Захардкожено в логике родительских задач**
   - `parent_id = "task_003"` в `check_and_complete_parent_tasks()` (строка ~266)
   - Предполагает, что dev_* задачи зависят от task_003
   - **Решение:** Переделать на основе явного `depends_on` или маршрутизации по типам

8. **Prod-риски безопасности**
   - Flask слушает `0.0.0.0:5000` без авторизации
   - CORS открыт (`CORS(app)`)
   - NocoDB-прокси дублирует только `limit/offset` (прочие params игнорируются — хорошо, но недокументировано)
   - **Решение:** Документировать, добавить логирование, рассмотреть простую авторизацию

### 🟡 СРЕДНИЙ ПРИОРИТЕТ

9. **Устаревший QA в utils.py**
   - `validate_with_qa()` (строки ~226–258) — старый формат
   - Сосуществует с новым Pydantic `run_qa_gate()`
   - **Решение:** Удалить, использовать только новый

10. **Parsинг категории в lead_tools**
    - По подстроке в описании (хрупкий паттерн)
    - **Решение:** Передавать категорию явно через input_data

11. **Тесты**
    - test_orchestrator (495 строк) — хороший объём
    - Но нет тестов integration: реального NocoDB, потока, цикла
    - **Решение:** Мокировать NocoDB полностью, добавить интеграционные тесты

---

## 9. Направления для углублённого анализа

### Направление A. Ядро оркестрации 🔴
**Точки входа:** `orchestrator.py`: `run` (405), `execute_task` (695), `_create_initial_task_graph` (547), `resolve_deadlock` (1393).

**Ключевые вопросы:**
- Корректна ли логика зависимостей и `_expand_completed_with_parents`?
- Нет ли race condition при `threaded=True`?
- Как обрабатываются циклы в графе (валидатор в `PMTaskGraph` строки ~80–96)?
- Насколько надёжна обработка застрявших задач (5 минут)?

---

### Направление B. Контракт с LLM 🔴
**Точки входа:** `utils.call_llm` (65), `schemas.extract_json_from_text` (539), `try_fix_truncated_json`.

**Ключевые вопросы:**
- Устойчивость к обрезанным/невалидным ответам?
- Стоит ли унифицировать 3 дублирующихся парсера?
- Как работает recovery `try_fix_truncated_json` (строки ~39–62)?

---

### Направление C. Модели данных и валидация 🟠
**Точки входа:** `schemas.py` (все Pydantic-модели), маппинг `AGENT_MODELS`.

**Ключевые вопросы:**
- Полнота схем vs реальные промпты?
- Сигнификант ли `extra="allow"` только в базовой модели?
- Совпадают ли JSON Schema в промптах с Python ожиданиями?

---

### Направление D. Persistence / NocoDB 🟠
**Точки входа:** `nocodb.py`, `main.py` nocodb_proxy (249), `config.get_nocodb_*`.

**Ключевые вопросы:**
- Разница API v2/v3 (`Id` vs `id`) — везде ли учтена?
- Идемпотентность обновлений задач?
- Безопасность прокси?

---

### Направление E. Промпты и поведение агентов 🟠
**Точки входа:** `prompts/*.txt`, спец-хендлеры `_handle_*`.

**Ключевые вопросы:**
- Соответствие промптов Pydantic-схемам?
- Почему часть агентов имеет спец-обработку?

---

### Направление F. Тесты, надёжность, безопасность 🟠
**Точки входа:** `tests/` (conftest 234, test_schemas 613), `.skills/security.md`.

**Ключевые вопросы:**
- Покрытие главного цикла? Мокируется ли сеть?
- Секьюрити-риски?

---

## 10. Рекомендации по дальнейшей работе

### Немедленно (Sprint 0)
- [ ] Унифицировать парсинг LLM → оставить одну реализацию в `schemas.py`
- [ ] Удалить дубль `_qa_response_to_result`
- [ ] Консолидировать `build_prompt_with_schema`
- [ ] Синхронизировать `TOKEN_BUDGET` (30000 vs 500000)

### Краткосрочно (Sprint 1)
- [ ] Разделить `orchestrator.py` на модули (QAGate, TaskExecutor, AgentHandlers)
- [ ] Документировать lead_tools: почему WB/Ozon закомментированы?
- [ ] Исправить `parent_id = "task_003"` на основе явных зависимостей
- [ ] Удалить `utils.validate_with_qa` (старый QA)

### Среднесрочно (Sprint 2–3)
- [ ] Добавить авторизацию/аутентификацию для API
- [ ] Интеграционные тесты (real-like NocoDB mock)
- [ ] Параллельное выполнение задач (если позволяют зависимости)
- [ ] Оптимизация prompts под Yandex AI

---

## 11. Быстрые ссылки

| Компонент | Файл | Строки |
|-----------|------|--------|
| Главный цикл | `orchestrator.py` | 405–536 |
| QA Gate | `orchestrator.py` | 1042–1210 |
| Pydantic-модели | `schemas.py` | 1–680 |
| LLM API wrapper | `utils.py` | 65–144 |
| Task Graph создание | `orchestrator.py` | 547–690 |
| Декомпозиция | `orchestrator.py` | 825–982 |
| NocoDB клиенты | `nocodb.py` | 1–454 |
| Поиск лидов | `lead_tools.py` | 1–314 |
| Дашборд | `index.html` | 1–1037 |

---

**Конец анализа**. Последнее обновление: Jul 1, 2026, 6:45 PM UTC+4.
