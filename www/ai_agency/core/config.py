import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    # NocoDB
    NOCODB_BASE_URL = os.getenv("NOCODB_BASE_URL", "http://localhost:8081").rstrip('/')
    NOCODB_API_TOKEN = os.getenv("NOCODB_API_TOKEN")
    NOCODB_BASE_ID = os.getenv("NOCODB_BASE_ID")
    NOCODB_TABLE_ID = os.getenv("NOCODB_TABLE_ID")  # agent_logs
    NOCODB_PROJECTS_TABLE_ID = os.getenv("NOCODB_PROJECTS_TABLE_ID")
    NOCODB_TASKS_TABLE_ID = os.getenv("NOCODB_TASKS_TABLE_ID")  # НОВОЕ
    # HTTP: таймаут одного запроса (сек); по умолчанию 1 минута
    NOCODB_TIMEOUT_SEC = int(os.getenv("NOCODB_TIMEOUT_SEC", "60"))
    # Всего попыток на запрос (включая первую). Default 4 = первая + 3 повтора
    # с паузами 10 / 30 / 60 сек (NOCODB_RETRY_DELAYS_SEC).
    NOCODB_MAX_ATTEMPTS = int(os.getenv("NOCODB_MAX_ATTEMPTS", "4"))
    # Паузы перед 2-й, 3-й и 4-й попыткой (сек). Env: "10,30,60"
    _nocodb_delays_raw = os.getenv("NOCODB_RETRY_DELAYS_SEC", "10,30,60")
    try:
        NOCODB_RETRY_DELAYS_SEC = tuple(
            int(x.strip()) for x in _nocodb_delays_raw.split(",") if x.strip()
        ) or (10, 30, 60)
    except ValueError:
        NOCODB_RETRY_DELAYS_SEC = (10, 30, 60)
    
    # LLM (мульти-провайдер: см. core/llm_engine.py и LLM_PROVIDER)
    # Активный провайдер: openai | grok | anthropic | deepseek | yandexgpt | gigachat
    LLM_PROVIDER = os.getenv("LLM_PROVIDER", "yandexgpt").strip().lower()
    # YandexGPT / общая совместимость (исторические имена)
    LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("YANDEX_API_KEY")
    LLM_BASE_URL = os.getenv(
        "LLM_BASE_URL", os.getenv("YANDEX_BASE_URL", "https://ai.api.cloud.yandex.net/v1")
    ).rstrip('/')
    LLM_FOLDER_ID = os.getenv("LLM_FOLDER_ID") or os.getenv("YANDEX_FOLDER_ID")
    LLM_MODEL = os.getenv("LLM_MODEL") or os.getenv("YANDEX_MODEL", "yandexgpt")
    
    # Limits
    TOKEN_BUDGET = int(os.getenv("TOKEN_BUDGET", 30000))
    MAX_TASK_ITERATIONS = int(os.getenv("MAX_TASK_ITERATIONS", 3))
    # Developer чаще требует доработки (сложные n8n workflow) — больший лимит по умолчанию.
    DEVELOPER_MAX_ITERATIONS = int(os.getenv("DEVELOPER_MAX_ITERATIONS", 6))

    # n8n version (читается из .env, используется в developer-промпте)
    N8N_VERSION = os.getenv("N8N_VERSION", "1.x")

    # n8n-validator: официальный движок (n8n-workflow + n8n-nodes-base)
    # auto — пробуем binary/npx; on — обязателен (fail если недоступен); off — только heuristic+local
    # Для релизов / CI рекомендуется N8N_VALIDATOR_OFFICIAL=on (Node >= 22).
    N8N_VALIDATOR_OFFICIAL = os.getenv("N8N_VALIDATOR_OFFICIAL", "auto").strip().lower()
    N8N_VALIDATOR_OFFICIAL_TIMEOUT = int(os.getenv("N8N_VALIDATOR_OFFICIAL_TIMEOUT", "120"))
    # Direction K в heuristic: ERROR на критичных HTTP/NocoDB без retry/error-ветки
    N8N_VALIDATOR_RESILIENCE = os.getenv("N8N_VALIDATOR_RESILIENCE", "on").strip().lower()
    # Опционально: instance-level n8n MCP (Builder validate_workflow — для SDK/TS кода).
    # Для JSON от developer основной путь — n8n-workflow-validator (см. n8n_validator.py).
    N8N_MCP_URL = os.getenv("N8N_MCP_URL", "").rstrip("/")
    N8N_MCP_ACCESS_TOKEN = os.getenv("N8N_MCP_ACCESS_TOKEN", "")

    # Telegram alerts (для уведомлений об ошибках оркестратора)
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

    # Google Custom Search API (client_hunter) — платный резервный источник (fallback)
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
    GOOGLE_CX = os.getenv("GOOGLE_CX", "")

    # OpenSERP — self-hosted бесплатный SERP API (https://github.com/karust/openserp).
    # Основной источник поиска клиентов для client_hunter: не требует платных ключей.
    # Поднимается локально/в сети агентства: docker run -p 7000:7000 karust/openserp serve.
    OPENSERP_BASE_URL = os.getenv("OPENSERP_BASE_URL", "http://localhost:7000")
    OPENSERP_ENGINE = os.getenv("OPENSERP_ENGINE", "google")
    # Клиентский HTTP-таймаут (сек). Серверный deadline — openserp/config.yaml → app.timeout.
    OPENSERP_TIMEOUT_SEC = int(os.getenv("OPENSERP_TIMEOUT_SEC", 300))
    # Повторы при 504/timeout/5xx (временные ошибки OpenSERP browser-поиска).
    OPENSERP_MAX_RETRIES = int(os.getenv("OPENSERP_MAX_RETRIES", 2))
    # После провала primary: /mega/search?mode=any и dedicated fallback.
    OPENSERP_FALLBACK_ENGINES = os.getenv(
        "OPENSERP_FALLBACK_ENGINES", "bing,yandex,duckduckgo"
    )
    OPENSERP_USE_MEGA_FALLBACK = os.getenv(
        "OPENSERP_USE_MEGA_FALLBACK", "true"
    ).strip().lower() in ("1", "true", "yes", "on")
    # После client_hunter: scrape website лида для email/телефона (открытые контакты).
    CLIENT_HUNTER_SCRAPE_CONTACTS = os.getenv(
        "CLIENT_HUNTER_SCRAPE_CONTACTS", "true"
    ).strip().lower() in ("1", "true", "yes", "on")
    CLIENT_HUNTER_SCRAPE_MAX = int(os.getenv("CLIENT_HUNTER_SCRAPE_MAX", 15))

    # Project defaults
    DEFAULT_PROJECT_NAME = os.getenv("DEFAULT_PROJECT_NAME", "Автоматизация WB")
    DEFAULT_CLIENT_NAME = os.getenv("DEFAULT_CLIENT_NAME", "ООО 'Ромашка' (Селлер WB)")
    DEFAULT_GOAL = os.getenv("DEFAULT_GOAL", "Автоматизировать сбор заявок с WB и создание задач в Bpium для ОКК.")

    @classmethod
    def get_llm_model_uri(cls) -> str:
        return f"gpt://{cls.LLM_FOLDER_ID}/{cls.LLM_MODEL}"

    @classmethod
    def get_llm_responses_url(cls) -> str:
        return f"{cls.LLM_BASE_URL}/responses"

    @classmethod
    def get_nocodb_records_url(cls) -> str:
        return f"{cls.NOCODB_BASE_URL}/api/v3/data/{cls.NOCODB_BASE_ID}/{cls.NOCODB_TABLE_ID}/records"

    @classmethod
    def get_nocodb_projects_url(cls) -> str:
        return f"{cls.NOCODB_BASE_URL}/api/v3/data/{cls.NOCODB_BASE_ID}/{cls.NOCODB_PROJECTS_TABLE_ID}/records"

    @classmethod
    def get_nocodb_tasks_url(cls) -> str:
        return f"{cls.NOCODB_BASE_URL}/api/v3/data/{cls.NOCODB_BASE_ID}/{cls.NOCODB_TASKS_TABLE_ID}/records"