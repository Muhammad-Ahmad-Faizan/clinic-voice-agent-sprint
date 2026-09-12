"""Application settings, loaded from environment variables (.env supported)."""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-driven configuration for the clinic voice agent backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        # .env files often carry extra vars (platform-injected, local tweaks);
        # ignore them instead of failing validation.
        extra="ignore",
    )

    # n8n webhook that fans out to the Google Calendar + notification workflows.
    n8n_webhook_url: str = ""

    # Dedicated n8n reschedule workflow: POSTs {existing_appointment_id,
    # new_date, new_time} and answers {"status": "success", "appointment_id"}
    # or {"status": "conflict"}.
    n8n_reschedule_webhook_url: str = ""

    # Optional SQLAlchemy database URL (Supabase/Neon/Render Postgres export
    # this as DATABASE_URL). When set, the structured decision log is written
    # to a `decision_log` table; otherwise the JSON-lines file
    # (DECISION_LOG_PATH) is used so local dev works without a database.
    database_url: str = ""

    # Shared secret Vapi sends on tool-call requests as the `x-vapi-secret`
    # header. Leave empty to disable the check (handy for local dev).
    vapi_webhook_secret: str = ""

    # Reference only: the LLM (Groq Llama-3.1) runs inside Vapi; this backend
    # never calls Groq directly. Kept here so the whole demo's env is in one place.
    groq_api_key: str = ""

    # HTTP port for uvicorn (Render/Railway inject PORT in deployment).
    port: int = 8000

    # Outbound timeout for n8n calls. Vapi expects tool results in ~2-3s, so
    # keep this tight (~5s) and fail loudly instead of hanging the phone call.
    n8n_timeout_seconds: float = 5.0

    # Max outbound retries for n8n calls. Only transient failures are retried
    # (timeouts/connection errors and n8n 502/503/504). A single retry keeps
    # the Vapi round-trip inside its ~2-3s budget.
    n8n_max_retries: int = 1

    # Append-only structured decision log (JSON Lines). Each line is one
    # decision point — book/reschedule/cancel/escalate/fail — and is what
    # GET /dashboard renders.
    decision_log_path: str = "data/decision_log.jsonl"


@lru_cache
def get_settings() -> Settings:
    """Cached accessor so the .env file is parsed once per process."""
    return Settings()


settings = get_settings()
