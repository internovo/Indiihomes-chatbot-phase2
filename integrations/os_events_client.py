"""Thin async client for indihomes-os's lead-events pipeline. Same shape
as integrations/lead_routing_client.py in this file (httpx.AsyncClient,
with_retry, lazy singleton) - read that file first if anything here looks
unfamiliar.

Pushes WhatsApp-template checkpoints (template_sent, delivered, failed,
resent) to indihomes-os's POST /api/lead-events, so the Lead Capture
screen's "AI Activity" tick and "Lead Journey" vertical tracker have real
data for Property Campaign / Generic Interest leads, not just direct-
website ones (see Indihomes-chatbot-V1's own os_events_client.py, the
sibling hook on that side of the system).

WHY THIS REUSES meta_delivery_store.py'S ALREADY-TRACKED STATES rather
than inventing new tracking: this repo already knows exactly when a send
succeeds (utils/sent_template_store.py), when WATI confirms delivery
(routes/webhook.py's sentMessageDELIVERED handling), and when it fails
(templateMessageFailed) - see utils/meta_delivery_store.py's module
docstring for the full design. emit() calls below are placed at those
EXISTING decision points, not a new parallel tracking mechanism.

==========================  S A F E T Y  ==========================
OS_EVENTS_DRY_RUN defaults to True (see config.py, same convention as
LEAD_ROUTING_DRY_RUN) - logs the payload, makes no call. Flip to False
only once indihomes-os's POST /api/lead-events is confirmed reachable
(its own server.cjs doesn't exist yet as of this writing - see that
repo's backend/LEAD_EVENTS_INTEGRATION.md).
===================================================================

emit_best_effort() never raises - every call site wraps campaign sends
and webhook handling that must never be disrupted by this integration.
"""
import httpx

from config import get_settings
from utils.logger import get_logger
from utils.retry import with_retry

logger = get_logger("os_events_client")


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.os_events_url)


class OsEventsClient:
    def __init__(self):
        settings = get_settings()
        self._base_url = settings.os_events_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if settings.os_events_shared_secret:
            headers["X-OS-Events-Secret"] = settings.os_events_shared_secret
        self._headers = headers
        self._timeout = settings.os_events_timeout_seconds

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._base_url, headers=self._headers, timeout=self._timeout)

    @with_retry(attempts=2)
    async def post_event(self, payload: dict) -> dict:
        async with await self._client() as client:
            resp = await client.post("/api/lead-events", json=payload)
            resp.raise_for_status()
            return resp.json()


_client_singleton: OsEventsClient | None = None


def get_os_events_client() -> OsEventsClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = OsEventsClient()
    return _client_singleton


async def emit_best_effort(
    phone: str,
    checkpoint: str,
    payload: dict | None = None,
    source_ref: str = "",
    idempotency_key: str = "",
) -> None:
    """Fire one WhatsApp checkpoint event, fire-and-forget. channel is
    always "whatsapp" - this repo has no voice concept.

    Never raises - every call site (campaign_service.py, routes/webhook.py)
    is on a path that must never be disrupted by this integration (a real
    customer send, or a webhook indihomes-os itself might be waiting on
    a 200 for).
    """
    if not phone:
        logger.info("os_events_client: no phone - skipping %s", checkpoint)
        return
    if not is_configured():
        # Silent no-op - expected state until OS_EVENTS_URL is set
        # (indihomes-os's server.cjs isn't restored yet - see this
        # module's docstring).
        return

    body = {
        "phone": phone,
        "channel": "whatsapp",
        "checkpoint": checkpoint,
        "payload": payload,
        "source_ref": source_ref or None,
        "idempotency_key": idempotency_key or None,
    }

    settings = get_settings()
    if settings.os_events_dry_run:
        logger.info("os_events_client DRY-RUN - would POST /api/lead-events (checkpoint=%s): %s", checkpoint, body)
        return

    try:
        client = get_os_events_client()
        await client.post_event(body)
        logger.info("os_events_client: %s emitted for phone=%s", checkpoint, phone)
    except Exception as exc:  # noqa: BLE001 - must never propagate, see module docstring
        logger.error("os_events_client: %s failed for phone=%s: %s", checkpoint, phone, exc)
