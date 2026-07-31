"""
Centralized configuration for the Phase 2 Campaign Service.
Everything is read from environment variables (see .env.example) so
secrets never live in code.
"""
from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # --- Existing IndiHomes backend ---
    indihomes_base_url: str = "https://api.indihomes.co.in/api/v1"
    indihomes_api_key: str = ""

    # --- WATI ---
    wati_api_key: str = ""
    wati_endpoint: str = "https://live-mt-server.wati.io"
    wati_template_name: str = "campaign_property_intro"

    # --- Worker tuning ---
    poll_interval_seconds: int = 45
    retry_worker_interval_seconds: int = 120
    cleanup_interval_seconds: int = 3600
    http_timeout_seconds: int = 15

    # How far back to look on the very first poll (no checkpoint yet)
    # or right after a manual reset_checkpoint(). Keeps a fresh
    # checkpoint from pulling the entire historical lead list.
    initial_lookback_hours: int = 24

    # --- Advisor email notifications (POST /notify-advisor) ---
    # Brevo's HTTPS API only - see integrations/email_client.py for why
    # (works identically locally and on Railway; SMTP does not).
    brevo_api_key: str = ""
    email_from: str = ""
    email_from_name: str = "Indihomes Bookings"
    advisor_emails: str = ""   # comma-separated, e.g. "a@x.com,b@x.com"
    notify_cc: str = ""        # comma-separated, optional oversight inbox(es)

    # --- Misc ---
    log_level: str = "INFO"
    environment: str = "development"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    """Settings are cached so we only parse the environment once."""
    return Settings()
