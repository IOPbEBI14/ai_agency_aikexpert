# 🤖 AI Agency OS — ИИ-Агентство Автоматизации E-commerce

**Версия 1.0** | Статус: Production Ready

ИИ-агентство для автоматизации бизнес-процессов e-commerce. Система из 9 специализированных ИИ-агентов, работающих по паттерну Supervisor-Workers, с автоматической декомпозицией задач, QA-проверкой и финализацией проектов.

---

## 🎯 Возможности

### Автоматизация полного цикла
- **Поиск лидов** — парсинг Telegram-каналов, Avito, 2GIS для поиска селлеров WB/Ozon
- **Продажи** — генерация персонализированных холодных сообщений
- **Анализ** — расчёт ROI, оценка экономии, подготовка КП
- **Архитектура** — проектирование интеграций (n8n + Bpium + API маркетплейсов)
- **Разработка** — создание JSON-схем workflow, скриптов, webhook-обработчиков
- **Настройка CRM** — проектирование полей, воронок, бизнес-процессов
- **Тестирование** — валидация JSON, проверка API, поиск логических ошибок
- **Документация** — создание инструкций, видео-скриптов, FAQ
- **Финализация** — сбор результатов, формирование отчёта для клиента

### Ключевые особенности
- ✅ **Supervisor-Workers оркестрация** — PM управляет 8 рабочими агентами
- ✅ **Task Graph с зависимостями** — автоматическое определение порядка задач
- ✅ **Декомпозиция задач** — PM разбивает сложные задачи на подзадачи
- ✅ **QA Gate** — каждый результат проверяется QA-агентом
- ✅ **Итеративная доработка** — до 3 попыток на задачу с учётом QA-фидбека
- ✅ **Human-in-the-Loop** — ручное вмешательство для критических задач
- ✅ **Stateless архитектура** — состояние хранится в NocoDB, можно перезапускать
- ✅ **Бюджет токенов** — автоматическая остановка при исчерпании бюджета
- ✅ **Финальный отчёт** — markdown-отчёт для клиента с метриками

---

## 🏗️ Архитектура

### Паттерн Supervisor-Workers

```
┌─────────────────────────────────────────────────────────────┐
│                    PM (Supervisor)                           │
│  • Декомпозирует задачи                                     │
│  • Распределяет между агентами                              │
│  • Контролирует выполнение                                  │
│  • Формирует финальный отчёт                                │
└────────────────┬────────────────────────────────────────────┘
                 │
    ┌────────────┼────────────┬────────────┬────────────┐
    │            │            │            │            │
    ▼            ▼            ▼            ▼            ▼
┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐
│ Analyst │ │Architect│ │Developer│ │   QA    │ │TechWriter│
└─────────┘ └─────────┘ └─────────┘ └─────────┘ └─────────┘
```

### Фазы проекта

1. **Инициация** — PM создаёт Task Graph
2. **Выполнение** — агенты выполняют задачи с учётом зависимостей
3. **QA Gate** — проверка результатов QA-агентом
4. **Финализация** — PM формирует отчёт для клиента

### Технологический стек

| Компонент | Технология | Назначение |
|-----------|-----------|------------|
| **LLM** | OpenAI / Grok / Anthropic / DeepSeek / YandexGPT / GigaChat | Генерация ответов агентов (выбор на дашборде) |
| **Оркестрация** | Python + FastAPI (uvicorn) | API и цикл оркестрации |
| **База данных** | NocoDB | Хранение проектов, задач, логов |
| **Дашборд** | HTML + JavaScript | Мониторинг и управление |
| **Проксирование** | FastAPI + Requests | Безопасная работа с NocoDB |

---

## 📁 Структура проекта

```
ai_agency/
├── main.py                      # FastAPI HTTP-слой (uvicorn)
├── core/api_schemas.py          # Pydantic-схемы REST API
├── core/api_payloads.py         # Сборка payload дашборда
├── core/
│   ├── config.py                # Конфигурация (env variables)
│   └── nocodb.py                # Клиенты для NocoDB
│       ├── NocoDBClient         # Работа с agent_logs
│       ├── ProjectsClient       # Работа с projects
│       └── TasksClient          # Работа с tasks
├── prompts/                     # Системные промпты агентов
│   ├── pm_prompt.txt            # Project Manager
│   ├── lead_hunter_prompt.txt   # Lead Hunter
│   ├── sales_prompt.txt         # Sales Agent
│   ├── analyst_prompt.txt       # Analyst
│   ├── architect_prompt.txt     # Architect
│   ├── developer_prompt.txt     # Developer
│   ├── crm_customizer_prompt.txt # CRM Customizer
│   ├── qa_prompt.txt            # QA Agent
│   └── tech_writer_prompt.txt   # Tech Writer
├── index.html                   # Дашборд (монохромный дизайн)
├── static/brand/                # Логотип, favicon, фирменные цвета (см. ниже)
├── .env                         # Конфигурация (не коммитить!)
└── README.md                    # Этот файл
```

### 🎨 Брендинг

Логотип и favicon — символ «A» с сетевым узлом внутри (метафора агентов, соединяющих
системы клиента). Файлы лежат в `static/brand/` и раздаются через
`app.mount("/static", ...)` в `main.py`. Подробности и палитра — в `ANALYSIS.md`
(раздел «Branding — логотип, favicon, фирменные цвета»).

| Токен | HEX | |
|---|---|---|
| `--brand-deep` | `#0f3b52` | ██ тёмно-синий |
| `--brand-teal` | `#0f5c4c` | ██ акцент (= `--accent` дашборда) |
| `--brand-cyan` | `#18b2c4` | ██ вторичный акцент |

### Таблицы NocoDB

#### `projects` — Проекты
| Поле | Тип | Описание |
|------|-----|----------|
| `project_name` | SingleLineText | Название проекта |
| `client_name` | SingleLineText | Имя клиента |
| `goal` | LongText | Цель проекта |
| `current_phase` | SingleLineText | Текущая фаза |
| `status` | SingleLineText | Статус (in_progress/completed/needs_human_review) |
| `tokens_used` | Number | Использовано токенов |
| `token_budget` | Number | Бюджет токенов |
| `final_report` | LongText | Финальный отчёт (markdown) |
| `metrics` | LongText | JSON с метриками |
| `completed_at` | DateTime | Дата завершения |

#### `tasks` — Задачи
| Поле | Тип | Описание |
|------|-----|----------|
| `task_id` | SingleLineText | Уникальный ID (task_001, dev_001) |
| `project_id` | Number | Связь с проектом |
| `agent_name` | SingleLineText | Агент-исполнитель |
| `task_description` | LongText | Описание задачи |
| `input_data` | LongText | JSON с входными данными (handoff) |
| `output_data` | LongText | JSON с результатом |
| `status` | SingleLineText | pending/in_progress/completed/failed |
| `depends_on` | LongText | JSON-массив зависимостей |
| `iteration_count` | Number | Количество итераций |
| `max_iterations` | Number | Максимум попыток (обычно 3) |
| `qa_approved` | SingleLineText | true/false/pending |
| `qa_feedback` | LongText | Комментарий QA |
| `tokens_used` | Number | Расход токенов |

#### `agent_logs` — Логи агентов
| Поле | Тип | Описание |
|------|-----|----------|
| `agent_name` | SingleLineText | Имя агента |
| `project_id` | Number | Связь с проектом |
| `status` | SingleLineText | Статус выполнения |
| `task_description` | LongText | Описание задачи |
| `full_response` | JSON | Полный ответ агента |
| `tokens_used` | Number | Расход токенов |
| `timestamp` | DateTime | Время выполнения |

---

## 🚀 Установка и запуск

### Требования

- Python 3.10+
- NocoDB (self-hosted или cloud)
- Yandex AI Studio API ключ
- Node.js (опционально, для дашборда)

### 1. Клонирование репозитория

```bash
git clone https://github.com/yourusername/ai-agency.git
cd ai-agency
```

### 2. Установка зависимостей

```bash
pip install -r requirements.txt
# или: python -m venv .venv && .venv\Scripts\pip install -r requirements.txt
```

### 3. Настройка `.env`

Создайте файл `.env` в корне проекта:

```env
# NocoDB Configuration
NOCODB_BASE_URL=http://localhost:8081
NOCODB_API_TOKEN=nc_pat_YOUR_TOKEN_HERE
NOCODB_BASE_ID=your_base_id
NOCODB_TABLE_ID=your_agent_logs_table_id
NOCODB_PROJECTS_TABLE_ID=your_projects_table_id
NOCODB_TASKS_TABLE_ID=your_tasks_table_id
# Надёжность HTTP (опционально): таймаут 60с; 4 попытки с паузами 10/30/60
# NOCODB_TIMEOUT_SEC=60
# NOCODB_MAX_ATTEMPTS=4
# NOCODB_RETRY_DELAYS_SEC=10,30,60

# Активный LLM при старте (можно сменить на дашборде без рестарта):
# openai | grok | anthropic | deepseek | yandexgpt | gigachat
LLM_PROVIDER=yandexgpt

# YandexGPT (обратная совместимость: LLM_* или YANDEX_*)
LLM_API_KEY=your_yandex_api_key
LLM_BASE_URL=https://ai.api.cloud.yandex.net/v1
LLM_FOLDER_ID=your_folder_id
LLM_MODEL=yandexgpt

# OpenAI (GPT-4o+ — автоматический prompt caching, настройка не нужна)
# OPENAI_API_KEY=sk-...
# OPENAI_MODEL=gpt-4o-mini
# OPENAI_BASE_URL=https://api.openai.com/v1

# Grok (xAI)
# GROK_API_KEY=xai-...
# GROK_MODEL=grok-2-latest
# GROK_BASE_URL=https://api.x.ai/v1

# Anthropic
# ANTHROPIC_API_KEY=sk-ant-...
# ANTHROPIC_MODEL=claude-sonnet-4-20250514

# DeepSeek
# DEEPSEEK_API_KEY=...
# DEEPSEEK_MODEL=deepseek-chat
# DEEPSEEK_BASE_URL=https://api.deepseek.com

# GigaChat (Authorization key / Basic credentials)
# GIGACHAT_CREDENTIALS=...
# GIGACHAT_MODEL=GigaChat
# GIGACHAT_SCOPE=GIGACHAT_API_PERS
# GIGACHAT_VERIFY_SSL=true

# Project Configuration
TOKEN_BUDGET=500000
# Логи: INFO по умолчанию; poll /status и рутина NocoDB не засоряют консоль
# LOG_LEVEL=INFO
# NOCODB_LOG_LEVEL=WARNING


# OpenSERP — self-hosted бесплатный SERP API (агент client_hunter, основной источник)
# https://github.com/karust/openserp — см. раздел "🔎 OpenSERP" ниже
# Обязательно монтировать openserp/config.yaml (app.timeout: 120), иначе 504 на Google
OPENSERP_BASE_URL=http://localhost:7000
OPENSERP_ENGINE=google
OPENSERP_TIMEOUT_SEC=300
OPENSERP_MAX_RETRIES=2
OPENSERP_FALLBACK_ENGINES=bing,yandex,duckduckgo
OPENSERP_USE_MEGA_FALLBACK=true

# Google Custom Search (агент client_hunter — резервный источник, если OpenSERP недоступен)
GOOGLE_API_KEY=your_google_api_key
GOOGLE_CX=your_custom_search_engine_id
MAX_TASK_ITERATIONS=3
DEVELOPER_MAX_ITERATIONS=6
# Official n8n-engine validator (Layer C): auto | on | off
N8N_VALIDATOR_OFFICIAL=auto
DEFAULT_PROJECT_NAME=Автоматизация WB
DEFAULT_CLIENT_NAME=ООО 'Ромашка' (Селлер WB)
DEFAULT_GOAL=Автоматизировать сбор отзывов с WB и создание задач в Bpium для ОКК.
```

### 🔎 OpenSERP (поиск клиентов, бесплатно, без API-ключей)

[OpenSERP](https://github.com/karust/openserp) — self-hosted SERP API (Google,
Yandex, Bing, DuckDuckGo, Baidu, Ecosia), используется агентом `client_hunter`
как основной источник поиска клиентов.

**Важно:** дефолтный `app.timeout=15` у OpenSERP часто даёт `504 context deadline
exceeded` на Google (browser). В агентстве лежит готовый конфиг
`openserp/config.yaml` с `timeout: 120` и `resilience.max_retries: 2`.

Запуск с конфигом агентства (рекомендуется):

```bash
cd www/ai_agency
docker run --rm -p 127.0.0.1:7000:7000 ^
  -v "%CD%/openserp/config.yaml:/config.yaml:ro" ^
  karust/openserp:latest serve --config /config.yaml
```

Linux/macOS:

```bash
docker run --rm -p 127.0.0.1:7000:7000 \
  -v "$PWD/openserp/config.yaml:/config.yaml:ro" \
  karust/openserp:latest serve --config /config.yaml
```

Проверка:

```bash
curl "http://127.0.0.1:7000/google/search?text=test&limit=5"
```

Клиент (`core/openserp_client.py`) при 504/timeout:
1. retry с backoff (до `OPENSERP_MAX_RETRIES`)
2. `/mega/search?mode=any` по fallback-движкам
3. затем Google Custom Search API (если настроены ключи)

```env
OPENSERP_BASE_URL=http://localhost:7000
OPENSERP_ENGINE=google
OPENSERP_TIMEOUT_SEC=300
OPENSERP_MAX_RETRIES=2
OPENSERP_FALLBACK_ENGINES=bing,yandex,duckduckgo
OPENSERP_USE_MEGA_FALLBACK=true
```

### 🛠 n8n-validator (корректная разработка workflow)

Перед QA каждый JSON от агента `developer` проходит **три слоя**:
1. Heuristic (Python) — всегда
2. Local `validate-n8n.js` — без npm
3. **Official** `n8n-workflow-validator` (движок n8n) — binary / `npx --yes`  
   Env: `N8N_VALIDATOR_OFFICIAL=auto|on|off` (по умолчанию `auto`)

Ошибки любого слоя блокируют задачу и возвращаются developer как `qa_feedback`.

```bash
cd www/ai_agency
node -v                    # нужно >= 22 (лучше 24); на v20 будет EBADENGINE)
npm run install-validator  # ставит official; xlsx берётся с npmjs (не cdn.sheetjs.com)
node validate-n8n.js --json path/to/workflow.json
npx n8n-workflow-validator --json path/to/workflow.json
# .env: N8N_VALIDATOR_OFFICIAL=on
```

**Типичные предупреждения `npm run install-validator`:**
| Сообщение | Значение |
|-----------|----------|
| `EBADENGINE … isolated-vm … required node >=22 … current v20` | **Нужно обновить Node** до 22+. Иначе native-модуль может не собраться. |
| `deprecated uuid / gm / glob / whatwg-encoding` | Шум от зависимостей n8n — **можно игнорировать**. |
| Зависание на `cdn.sheetjs.com` / `xlsx` | В `package.json` уже есть `"overrides": {"xlsx":"0.18.5"}` с registry.npmjs.org. Удалите `node_modules` + `package-lock.json` и повторите `npm run install-validator`. |

Подробности — `ANALYSIS.md` → Direction N / O.

### 4. Создание таблиц в NocoDB

Создайте три таблицы согласно структуре выше:
- `projects`
- `tasks`
- `agent_logs`

### 5. Запуск сервера

```bash
python main.py
# или: uvicorn main:app --host 0.0.0.0 --port 5000
# Swagger UI: http://localhost:5000/docs
```

Сервер запустится на `http://localhost:5000`

### 6. Открытие дашборда

Откройте `http://localhost:5000` в браузере или настройте проксирование через Caddy/Nginx.

---

## 📊 Использование

### Запуск проекта

1. Откройте дашборд
2. Нажмите **"▶ Запустить агентство"**
3. PM создаст Task Graph и начнёт выполнение задач
4. Наблюдайте за прогрессом в реальном времени

### Управление проектом

- **⏹ Остановить** — пауза выполнения
- **↻ Продолжить анализ** — возобновление после остановки
- **+ Увеличить токены** — добавление бюджета (по начальному значению)
- **↻ Обновить** — принудительное обновление данных

### Human-in-the-Loop

Если задача переходит в статус `needs_human_review`:
1. На карточке появляется бейдж **"REVIEW"**
2. Кликните на карточку
3. Введите промпт для PM
4. PM скорректирует задачу и переведёт её в `pending`

### Просмотр финального отчёта

После завершения проекта:
1. На дашборде появляется карточка **"📋 Финальный отчет проекта"**
2. Отчёт содержит:
   - Краткое резюме
   - Что было сделано (по каждому агенту)
   - Технические детали
   - Рекомендации
   - Метрики (токены, время, итерации)
3. Можно скачать отчёт в формате `.md`

---

## 🔧 Конфигурация

### Бюджет токенов

- **Начальный бюджет**: 500,000 токенов (настраивается в `.env`)
- **Автоматическая остановка**: при использовании 80% бюджета
- **Увеличение бюджета**: кнопка "+ Увеличить токены" добавляет начальное значение

### Лимит итераций

- **Максимум итераций на задачу**: 3 (настраивается в `.env`)
- **После превышения**: задача переходит в `failed`
- **QA-фидбек**: передаётся агенту для исправления ошибок

### Промпты агентов

Все промпты хранятся в папке `prompts/` в формате `.txt`. Можно редактировать для настройки поведения агентов.

---

## 📈 Мониторинг

### Логи

Логи выводятся в консоль:

```bash
python main.py 2>&1 | tee agency.log
```

### Метрики

В таблице `projects` хранятся метрики:
- `tokens_used` — общие затраты токенов
- `metrics` — JSON с детализацией по агентам и задачам
- `completed_at` — время завершения

### Дашборд

Дашборд обновляется каждые 10 секунд автоматически. Показывает:
- Статус проекта
- Расход токенов (с прогресс-баром)
- Активные агенты
- Карточки с результатами работы агентов
- Финальный отчёт (после завершения)

---

## 🛠️ Разработка

### Добавление нового агента

1. Создайте промпт в `prompts/new_agent_prompt.txt`
2. Добавьте агента в `build_initial_task_graph()` в `main.py`
3. Добавьте обработку в `renderAgentContent()` в `index.html`

### Изменение оркестрации

Основная логика в функции `run_agency()` в `main.py`:
- Цикл выполнения задач
- Проверка зависимостей
- Вызов агентов
- QA Gate
- Финализация

### Добавление новых таблиц

1. Создайте таблицу в NocoDB
2. Добавьте клиент в `core/nocodb.py`
3. Добавьте URL в `core/config.py`
4. Используйте клиент в `main.py`

---

## 🐛 Troubleshooting

### Ошибка `Invalid URL 'None'`

**Причина**: `project_name` не загружен из NocoDB.

**Решение**: Проверьте, что в таблице `projects` есть поле `project_name` и оно не пустое.

### Ошибка `422 Unprocessable Entity`

**Причина**: Невалидный JSON в поле `full_response` (тип JSON в NocoDB).

**Решение**: Функция `log_to_agent_logs()` автоматически конвертирует текст в JSON. Проверьте, что поле `full_response` имеет тип **JSON** в NocoDB.

### Агент зацикливается

**Причина**: PM не видит завершённые задачи.

**Решение**: Проверьте, что `_check_and_complete_parent_tasks()` корректно помечает родительские задачи как `completed`.

### Обрезанные ответы

**Причина**: YandexGPT не успевает сгенерировать полный ответ.

**Решение**: Функция `call_llm()` автоматически повторяет запрос до 2 раз и пытается восстановить JSON через `try_fix_truncated_json()`.

---

## 📝 Changelog

### v1.0 (2026-06-23)

**Первый стабильный релиз**

#### Реализовано
- ✅ Supervisor-Workers оркестрация
- ✅ 9 специализированных агентов
- ✅ Task Graph с зависимостями
- ✅ Декомпозиция задач после architect
- ✅ QA Gate с итеративной доработкой
- ✅ Human-in-the-Loop для критических задач
- ✅ Stateless архитектура (NocoDB)
- ✅ Бюджет токенов с автоматической остановкой
- ✅ Финальный отчёт в markdown
- ✅ Монохромный дашборд
- ✅ Полное логирование в agent_logs

#### Известные ограничения
- Параллельное выполнение задач не реализовано (последовательное выполнение)
- Нет экспорта результатов в внешние системы (только NocoDB)
- Нет интеграции с реальными API маркетплейсов (только генерация кода)

---

## 🤝 Contributing

Приветствуются:
- Баг-репорты
- Предложения по улучшению
- Pull requests

Перед созданием PR:
1. Проверьте код через `python -m py_compile main.py`
2. Убедитесь, что все тесты проходят
3. Обновите документацию

---

## 📄 Лицензия

MIT License

---

## 👥 Авторы

- **Разработка**: AI Agency Team
- **Дизайн дашборда**: Вдохновлено стилем Andrei Rybin

---

## 📞 Поддержка

- **Email**: admin@agency.local
- **Документация**: [Wiki](https://github.com/yourusername/ai-agency/wiki)
- **Issues**: [GitHub Issues](https://github.com/yourusername/ai-agency/issues)

---

## 🙏 Благодарности

- **Yandex AI Studio** — за мощный LLM API
- **NocoDB** — за гибкую open-source базу данных
- **FastAPI** — за асинхронный API и OpenAPI из коробки
- **Сообществу** — за обратную связь и тестирование

---

**Версия 1.0** | Последнее обновление: 23 июня 2026
