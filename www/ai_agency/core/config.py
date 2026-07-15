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

    # Telegram alerts (для уведомлений об ошибках оркестратора)
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

    # Google Custom Search API (client_hunter)
    GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
    GOOGLE_CX = os.getenv("GOOGLE_CX", "")

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