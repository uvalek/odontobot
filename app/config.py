from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openai_api_key: str
    openai_model_brain: str = "gpt-5.4-nano"
    openai_model_catalog: str = "gpt-5.4-nano"
    openai_model_vision: str = "gpt-5.4-nano"
    # Solo aplica a modelos de razonamiento (gpt-5 / o-series). En modelos
    # clásicos (gpt-4.1/4o) se ignora. Override con OPENAI_REASONING_EFFORT.
    openai_reasoning_effort: str = "low"
    # Piso de max_completion_tokens para que el "pensamiento" interno no deje
    # la respuesta vacía (el router pedía solo 4 tokens).
    openai_reasoning_max_tokens_floor: int = 2000

    supabase_url: str
    supabase_service_key: str
    supabase_anon_key: str = ""
    supabase_rag_table: str = "documents"
    supabase_rag_query: str = "match_documents"

    cal_api_key: str = ""
    cal_event_type_id: int = 0

    telegram_bot_token: str = ""
    telegram_webhook_secret: str = ""

    manychat_api_token: str = ""

    buffer_window_seconds: int = 30
    # Intervalo del reaper de huerfanos (segundos). Es solo un respaldo
    # por si schedule_flush se muere; el flujo normal procesa en `buffer_window_seconds`.
    reaper_interval_seconds: int = 60
    memory_turns: int = 25
    send_delay_seconds: float = 1.0
    timezone: str = "America/Mexico_City"

    log_level: str = "INFO"

    # ManyChat test mode (estilo n8n "Execute Workflow")
    manychat_require_arm: bool = True
    test_arm_token: str = ""

    # Dashboard CRM externo (Vite/React en Vercel)
    # API key compartido (header X-API-Key) y origenes permitidos para CORS.
    # Acepta lista separada por comas: "https://a.com,https://b.vercel.app"
    dashboard_api_key: str = ""
    dashboard_cors_origins: str = "http://localhost:5173,http://localhost:3000"
    # Por defecto sin regex (estaba aceptando *.vercel.app y eso permitia
    # a cualquiera deployar a Vercel y consumir el endpoint). Si se
    # necesita matchear varios subdominios, definir explicitamente con
    # la variable de entorno DASHBOARD_CORS_ORIGIN_REGEX.
    dashboard_cors_origin_regex: str = ""

    # --- Webchat (widget en alekagency.com) ----------------------------
    # Si se define, /api/webchat exige el header X-API-Key con este valor.
    # Asi el proxy de Next.js es el unico que puede llegar al endpoint.
    webchat_api_key: str = ""
    # Rate limit por IP (sliding window). 0 desactiva.
    webchat_rate_limit_per_min: int = 30
    # Limite de longitud del texto entrante (ya se truncaba a 2000;
    # ahora se rechaza el request si excede mucho).
    webchat_max_text_len: int = 2000
    # Origenes permitidos para el widget (CORS, header Origin).
    # Vacio = no se restringe por CORS (cualquier dominio puede hacer
    # cross-origin). Util para llamadas server-to-server, que es
    # exactamente nuestro caso (Next.js proxy).
    webchat_cors_origins: str = ""

    # --- Endurecido de webhooks ----------------------------------------
    # ManyChat no firma sus External Requests. Para evitar que cualquiera
    # mande POSTs falsos al webhook, definir esta variable y configurar
    # ManyChat para enviar el mismo valor en el header X-ManyChat-Secret.
    # Vacio = no se exige (compatibilidad hacia atras).
    manychat_webhook_secret: str = ""
    # Tamaño máximo del body de cualquier request (bytes).
    max_request_body_bytes: int = 1024 * 1024  # 1 MB

    # --- Anti-abuso por conversación -----------------------------------
    # Rate limit por chat_id (cualquier canal). 0 desactiva.
    chat_rate_limit_per_min: int = 20
    chat_rate_limit_per_day: int = 500
    # Presupuesto de tokens por chat por día. 0 desactiva.
    chat_token_budget_per_day: int = 80000
    # Circuit breaker global: si el bot pasa este gasto diario, para todo
    # hasta el reset (UTC). 0 desactiva.
    global_token_budget_per_day: int = 2000000
    # Eventos sospechosos en ventana de 10 minutos antes de bloquear el chat.
    suspicious_block_threshold: int = 5

    @property
    def prompts_dir(self) -> Path:
        return Path(__file__).parent / "prompts"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def load_prompt(name: str) -> str:
    return (get_settings().prompts_dir / f"{name}.md").read_text(encoding="utf-8")
