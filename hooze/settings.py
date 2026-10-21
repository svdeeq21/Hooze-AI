"""Platform-level settings (one deployment serves many tenants).

Extracted from: pd config.py + savoury config.py. What changed:
  * Every per-client value (CLIENT_NAME, CLIENT_MIN_BUDGET_NAIRA, ORG_SLUG,
    ADMIN_WHATSAPP_NUMBER, EVOLUTION_INSTANCE_NAME, PAYSTACK_SECRET_KEY, ...)
    is GONE from the environment. Those live in the database, per tenant
    (channels / tenant_configs / encrypted credentials).
  * Only platform secrets remain here.
  * Nothing is required at import time (both old apps crashed on import if a
    key was missing, which makes tests need placeholder env vars).
    `assert_production_ready()` is called from the app factory instead.
"""
from __future__ import annotations
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")

    app_env: str = "production"
    public_base_url: str = ""         # e.g. https://api.hooze.app — used to build per-channel webhook URLs
    log_level: str = "INFO"
    # durable inbound queue
    inbound_max_attempts: int = 5               # then the message is marked failed and an alert fires
    inbound_lease_seconds: int = 180            # a worker that dies mid-turn is recovered after this long
    inbound_recovery_interval_seconds: int = 30
    inbound_recovery_min_age_seconds: int = 20  # never steal a row the webhook's own task is about to process
    inbound_max_concurrency: int = 10           # simultaneous turns in this process (protects LLM rate limits)
    inbound_retention_days: int = 7
    # alerts (all optional; with none configured, alerts are only logged)
    alert_whatsapp_numbers: str = ""            # comma-separated, digits with country code, e.g. 2348012345678
    alert_channel_id: str = ""                  # which WhatsApp number SENDS alerts (default: the first active one). Use a dedicated ops number.
    alert_email_to: str = ""                    # comma-separated
    alert_email_from: str = "Hooze Alerts <alerts@hoozeenterprises.com>"
    resend_api_key: str = ""
    alert_dedupe_minutes: int = 30
    maintenance_enabled: bool = True            # run the recovery/monitor loop inside this process
    channel_check_interval_seconds: int = 120
    cors_allowed_origins: str = ""   # comma-separated browser origins allowed to call the API (the dashboard). Empty = no browser access.

    # Storage
    media_bucket: str = "media"          # Supabase Storage bucket for uploaded menu photos/videos (created public on first upload)
    media_max_image_mb: int = 15         # per uploaded photo (before resizing)
    media_max_video_mb: int = 16         # WhatsApp's own video limit
    media_image_max_px: int = 1600       # photos are shrunk to fit this on the longest side
    supabase_url: str = ""
    supabase_service_role_key: str = ""

    # LLM providers (platform-owned keys; per-tenant BYO keys are a Tier-B feature)
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    embedding_model: str = "models/gemini-embedding-2-preview"
    embedding_dims: int = 768
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-120b"
    llm_timeout_seconds: float = 20.0
    llm_provider_order: str = "gemini,groq"       # try providers in this order; put the one that is working best FIRST
    groq_reasoning_effort: str = ""               # "low" makes gpt-oss answer faster and stops it spending its whole token budget thinking ("" = don't send)
    llm_outage_attempts: int = 3                  # when EVERY provider fails: hold + retry this many times, then hand to a human
    channel_reemit_seconds: int = 1800            # a WhatsApp number that stays disconnected is re-reported at most this often
    llm_provider_cooldown_seconds: float = 30.0   # a provider that just failed is skipped this long (0 disables)

    # Platform auth
    platform_admin_key: str = ""      # can create tenants / channels
    internal_cron_secret: str = ""    # protects /internal/*
    vault_master_key: str = ""        # Fernet key; encrypts per-tenant credentials at rest

    # Shared state (optional on a single worker, REQUIRED on >1 worker)
    upstash_redis_rest_url: str = ""
    upstash_redis_rest_token: str = ""

    # Global kill switches (flip in env, no deploy) — pd's follow_up_engine_enabled, now platform-wide
    nudges_globally_enabled: bool = True

    # Runtime defaults (a tenant's RuntimeConfig can override the ones that make sense)
    history_window: int = 24
    summary_interval: int = 5
    typing_delay_min: float = 1.0
    typing_delay_max: float = 2.5

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    def assert_production_ready(self) -> None:
        missing = [k for k in ("supabase_url", "supabase_service_role_key", "gemini_api_key",
                               "platform_admin_key", "internal_cron_secret", "vault_master_key")
                   if not getattr(self, k)]
        if missing:
            raise RuntimeError(f"Missing required settings: {', '.join(missing)}")


@lru_cache
def get_settings() -> Settings:
    return Settings()
