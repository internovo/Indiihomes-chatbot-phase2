"""Thin async wrapper around the NEW indihomes-lead-routing-service
(Phase 3). Same shape as integrations/wati_client.py and
integrations/indihomes_client.py in this file - httpx.AsyncClient,
with_retry, a lazy singleton getter.

Only called from services/campaign_service.py::process_lead(), and
only for Property Campaign leads (project_code/project_name present) -
Generic Interest leads have no project to resolve a salesperson for,
so process_generic_lead() never calls this.

route_lead() never raises to its caller - see
notify_lead_routing_best_effort() below, which is the function
campaign_service actually calls. Same "a side effect must never
retrigger a resend" discipline as _update_crm_status_after_send in
campaign_service.py: this fires AFTER the WATI send has already
succeeded, so a routing-service failure here must never change
record.status back to something retryable.
"""
import httpx

from config import get_settings
from models.lead import Lead
from models.property import Property
from utils.logger import get_logger
from utils.retry import with_retry

logger = get_logger("lead_routing_client")


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.lead_routing_url and settings.lead_routing_shared_secret)


class LeadRoutingClient:
    def __init__(self):
        settings = get_settings()
        self._base_url = settings.lead_routing_url.rstrip("/")
        self._headers = {
            "Content-Type": "application/json",
            "X-Lead-Routing-Secret": settings.lead_routing_shared_secret,
        }
        self._timeout = settings.lead_routing_timeout_seconds

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._base_url, headers=self._headers, timeout=self._timeout)

    @with_retry(attempts=2)
    async def save_and_route(self, payload: dict, idempotency_key: str) -> dict:
        async with await self._client() as client:
            resp = await client.post(
                "/api/v1/leads/save-and-route",
                json=payload,
                headers={"X-Idempotency-Key": idempotency_key},
            )
            # 207 (partial success) is a normal outcome, not an error -
            # only 4xx/5xx-that-aren't-207 should raise for the retry
            # decorator to act on.
            if resp.status_code not in (200, 207):
                resp.raise_for_status()
            return resp.json()


_client_singleton: LeadRoutingClient | None = None


def get_lead_routing_client() -> LeadRoutingClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = LeadRoutingClient()
    return _client_singleton


def _build_payload(lead: Lead, prop: Property) -> dict:
    """Maps a resolved (Lead, Property) pair to the routing service's
    canonical contract (Indihomes_WATI_Salesperson_Routing_Implementation_Plan.docx
    section 5). `source` is always "meta_ads" here - process_generic_lead
    never calls this (see module docstring), and process_lead is only
    reached for leads that already carry project data, regardless of
    their original CRM lead_source string (Housing.com, Meta Ads, etc -
    see services/lead_service.py's classify_lead() docstring on why
    classification is data-presence based, not source-string based).
    "meta_ads" here just means "came through the Phase 2 pipeline",
    matching the plan's own two-source vocabulary (direct_website /
    meta_ads)."""
    return {
        "source": "meta_ads",
        "phone": lead.phone or "",
        "name": lead.name or "",
        "project_codes": [prop.project_code] if prop.project_code else [],
        "outcome": "details_shared",
        "source_lead_id": lead.id,
    }


async def notify_lead_routing_best_effort(lead: Lead, prop: Property) -> None:
    """Fire-and-forget call to the routing service. Swallows every
    exception and logs loudly instead - same contract as
    campaign_service._update_crm_status_after_send: this runs AFTER the
    WATI send to the CUSTOMER already succeeded, so nothing here may
    ever cause that to be retried (a retry would re-send the customer's
    opening template, not just re-notify the salesperson)."""
    if not is_configured():
        logger.info("LEAD_ROUTING_URL not configured - skipping salesperson notification for lead %s", lead.id)
        return
    if not prop.project_code:
        logger.info("Lead %s resolved a property with no project_code - nothing to route", lead.id)
        return

    payload = _build_payload(lead, prop)
    idempotency_key = f"meta_ads:{lead.id}"

    try:
        client = get_lead_routing_client()
        result = await client.save_and_route(payload, idempotency_key)
        logger.info("Salesperson routing notified for lead %s: %s", lead.id, result)
    except Exception as exc:  # noqa: BLE001 - see docstring; must never propagate
        logger.error(
            "Salesperson routing notification failed for lead %s (NOT retrying via the campaign "
            "retry queue - that would re-send the customer's own template, not just re-notify "
            "the salesperson): %s",
            lead.id, exc,
        )
