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
    
    # LLM
    LLM_API_KEY = os.getenv("LLM_API_KEY")
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://ai.api.cloud.yandex.net/v1").rstrip('/')
    LLM_FOLDER_ID = os.getenv("LLM_FOLDER_ID")
    LLM_MODEL = os.getenv("LLM_MODEL", "yandexgpt")
    
    # Limits
    TOKEN_BUDGET = int(os.getenv("TOKEN_BUDGET", 30000))
    MAX_TASK_ITERATIONS = int(os.getenv("MAX_TASK_ITERATIONS", 3))
    # Developer чаще требует доработки (сложные n8n workflow) — больший лимит по умолчанию.
    DEVELOPER_MAX_ITERATIONS = int(os.getenv("DEVELOPER_MAX_ITERATIONS", 6))

    # n8n version (читается из .env, используется в developer-промпте)
    N8N_VERSION = os.getenv("N8N_VERSION", "1.x")

    # n8n-validator: официальный движок (n8n-workflow + n8n-nodes-base)
    # auto — пробуем binary/npx; on — обязателен (fail если недоступен); off — только heuristic+local
    N8N_VALIDATOR_OFFICIAL = os.getenv("N8N_VALIDATOR_OFFICIAL", "auto").strip().lower()
    N8N_VALIDATOR_OFFICIAL_TIMEOUT = int(os.getenv("N8N_VALIDATOR_OFFICIAL_TIMEOUT", "120"))
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
    OPENSERP_TIMEOUT_SEC = int(os.getenv("OPENSERP_TIMEOUT_SEC", 30))

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