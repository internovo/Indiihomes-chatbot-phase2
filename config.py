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
    # Property Campaign leads (Housing.com / website form) - references a specific project.
    wati_template_name: str = "campaign_property_intro"
    # Generic Interest leads (Meta Ads / EOI) - no project data, just a
    # "thanks for your interest" opener that hands off into Phase 1's
    # qualification flow once the lead taps in.
    wati_generic_template_name: str = "campaign_generic_intro"

    # --- Worker tuning ---
    poll_interval_seconds: int = 45
    retry_worker_interval_seconds: int = 120
    cleanup_interval_seconds: int = 3600
    http_timeout_seconds: int = 15

    # How far back to look on the very first poll (no checkpoint yet)
    # or right after a manual reset_checkpoint(). Keeps a fresh
    # checkpoint from pulling the entire historical lead list.
    initial_lookback_hours: int = 24

    # --- Business-hours gating (see business_hours.py,
    # services/campaign_service.py, utils/pending_queue.py) ---
    # When the daily off-hours-queue flush runs. Defaults to
    # business_hours.BUSINESS_START (10:00 AM IST) DELIBERATELY, not the
    # "9:00 AM" text in Indihomes_Business_Hours_Gating.docx §3.3 - that
    # doc contradicts itself (its own code comment says "10:00 AM IST
    # daily" over an hour=9 value, and §4 "End-to-End Flow" says
    # "10:00 AM IST" outright). Flushing exactly AT business open, not an
    # hour before it, is also the only choice that doesn't itself
    # violate the gate: is_business_hours() would still read False at
    # 9:00 AM, so a 9 AM flush job would immediately re-queue everything
    # it just tried to send. See claude.md, "Business hours gating", for
    # the full explanation. Configurable here (not hardcoded in the
    # worker) exactly like every other worker interval in this file, in
    # case business hours themselves ever change.
    queue_flush_hour_ist: int = 10
    queue_flush_minute_ist: int = 0

    # --- Advisor email notifications (POST /notify-advisor) ---
    # Brevo's HTTPS API only - see integrations/email_client.py for why
    # (works identically locally and on Railway; SMTP does not).
    brevo_api_key: str = ""
    email_from: str = ""
    email_from_name: str = "Indihomes Bookings"
    advisor_emails: str = ""   # comma-separated, e.g. "a@x.com,b@x.com"
    notify_cc: str = ""        # comma-separated, optional oversight inbox(es)

    # --- Phase 3: indihomes-lead-routing-service (salesperson notification) ---
    # Blank lead_routing_url = the Phase 3 hook in campaign_service.py is a
    # silent no-op, same convention as every other optional integration in
    # this file. lead_routing_dry_run mirrors wati_client's own safety
    # switch on the routing service side - keep this true until that
    # service's own WATI_DRY_RUN has separately been confirmed safe to
    # disable (see indihomes-lead-routing-service/README.md).
    lead_routing_url: str = ""
    lead_routing_shared_secret: str = ""
    lead_routing_dry_run: bool = True
    lead_routing_timeout_seconds: int = 15

    # Defaults FALSE - see claude.md, "Meta Ads salesperson notification
    # disabled" (2026-08-12), and understand.md §14 for the full story.
    # models/lead.py has no configuration/budget fields at all - this
    # repo's Lead model never captured them from the CRM in the first
    # place, so every Meta Ads salesperson notification sent so far went
    # out with "Looking For: -" and "Budget: -" (confirmed from real
    # production messages). A notification missing the two most
    # decision-relevant fields is worse than no notification - it reads
    # as broken software to the salesperson receiving it, not "no data
    # yet". Flip to true only once models/lead.py + lead_routing_client.py
    # actually capture and forward real configuration/budget values from
    # the CRM's raw lead payload (confirm the real field names first -
    # see scripts/dump_get_new_leads_raw.py). Env var:
    # LEAD_ROUTING_META_ADS_ENABLED=true (matches this field name exactly -
    # pydantic-settings maps snake_case field -> SCREAMING_SNAKE env var).
    lead_routing_meta_ads_enabled: bool = False

    # --- Misc ---
    log_level: str = "INFO"
    environment: str = "development"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    """Settings are cached so we only parse the environment once."""
    return Settings()
