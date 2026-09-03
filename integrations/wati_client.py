"""Thin async wrapper around the WATI API. Only what the campaign flow
actually needs: sending a pre-approved template (to open the
conversation with a lead who hasn't messaged us first) and a couple of
light contact-management helpers that are cheap to include.

send_template uses the v2 endpoint - v1 was returning a generic
{"result": false, "info": "Check your template, it cannot have typos
or blank text"} error on every single send attempt (both approved
templates, multiple recipients, after confirming the tenant-ID-suffixed
endpoint and API key were both correct) as of 3 Aug 2026. WATI's own
help docs (support.wati.io, "How to track template message delivery...")
explicitly point to v2 as the current endpoint; v1 appears to still
exist but not be reliably functional. Response shape differs
(v2: {"result", "error", "templateName", "receivers"} vs v1's
{"result", "info", "validWhatsAppNumber"}) but nothing in this codebase
reads specific fields off the return value, so this is a safe swap.
"""
from datetime import datetime, timezone

import httpx

from config import get_settings
from utils.logger import get_logger
from utils.retry import with_retry

logger = get_logger("wati_client")

# --- Credential health --------------------------------------------------
#
# Added 3 Sep 2026, after the incident this whole module's docstring
# above is a monument to a smaller version of.
#
# WATI access tokens are JWTs with a hard expiry. When this one expired
# (~28 Aug 2026), every sendTemplateMessage returned 401. Nothing in the
# pipeline treated that differently from a network blip: with_retry
# retried 3 times, campaign_service marked the lead RETRYING,
# retry_worker retried 3 more times over ~80 minutes, then abandoned it
# and emailed an advisor. Repeat, per lead, for five days. /health said
# "ok" the entire time, no worker crashed, and the only trace was a
# WARNING line in logs that roll off after ~3 hours.
#
# A 401/403 is categorically different from a timeout: it is
# account-wide, affects EVERY send, and cannot clear on its own - a
# human has to rotate the token. So it gets recorded here and surfaced
# on /health, where "the pipeline is healthy but sending nothing" stops
# being an invisible state.
#
# Deliberately NOT changed: the retry behaviour itself. Short-circuiting
# sends on a remembered auth failure would risk one bad response
# wedging a working key, and the loud ERROR + /health field already
# solve the actual problem, which was never the wasted retries - it was
# nobody knowing.
_auth_state: dict = {"status": "unknown", "detail": "", "at": None}


def _note_auth(ok: bool, detail: str = "") -> None:
    _auth_state["status"] = "ok" if ok else "REJECTED"
    _auth_state["detail"] = detail
    _auth_state["at"] = datetime.now(timezone.utc).isoformat()


def auth_status() -> dict:
    """Whether WATI last accepted or rejected this service's credentials.

    "unknown" until the first send is attempted - a freshly restarted
    service has not proven anything either way yet. Read by
    routes/health.py.
    """
    return dict(_auth_state)


class WatiClient:
    def __init__(self):
        settings = get_settings()
        self._base_url = settings.wati_endpoint.rstrip("/")
        self._headers = {
            "Content-Type": "application/json-patch+json",
            "Authorization": f"Bearer {settings.wati_api_key}",
        }
        self._timeout = settings.http_timeout_seconds

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._base_url, headers=self._headers, timeout=self._timeout)

    @with_retry(attempts=3)
    async def send_template(self, phone: str, template_name: str, parameters: list[dict]) -> dict:
        """parameters is WATI's expected shape: [{"name": "name", "value": "..."}]"""
        async with await self._client() as client:
            resp = await client.post(
                f"/api/v2/sendTemplateMessage?whatsappNumber={phone}",
                json={"template_name": template_name, "broadcast_name": template_name, "parameters": parameters},
            )
            if resp.status_code in (401, 403):
                # See the _auth_state block comment at the top of this
                # module. This is the line that would have turned a
                # five-day silent outage into a five-minute one.
                _note_auth(False, f"HTTP {resp.status_code} from sendTemplateMessage")
                logger.error(
                    "WATI REJECTED THIS SERVICE'S CREDENTIALS (HTTP %s). This is NOT a transient failure - "
                    "the access token has almost certainly expired, and EVERY campaign send will keep failing "
                    "until it is rotated (WATI -> Settings -> API & Webhooks -> Access Token, then update "
                    "WATI_API_KEY in Railway). Check GET /health's wati_auth field.",
                    resp.status_code,
                )
            resp.raise_for_status()
            _note_auth(True)
            return resp.json()

    @with_retry(attempts=2)
    async def send_message(self, phone: str, message: str) -> dict:
        async with await self._client() as client:
            resp = await client.post(f"/api/v1/sendSessionMessage/{phone}", params={"messageText": message})
            resp.raise_for_status()
            return resp.json()

    @with_retry(attempts=2)
    async def tag_contact(self, phone: str, tags: list[str]) -> dict:
        async with await self._client() as client:
            resp = await client.post(f"/api/v1/addTagsToContact/{phone}", json={"tags": tags})
            resp.raise_for_status()
            return resp.json()

    @with_retry(attempts=2)
    async def update_contact_attributes(self, phone: str, attributes: dict) -> dict:
        """Sets custom contact attributes in WATI (e.g. last_campaign_template)
        so downstream Automation Rules can filter on them - see
        claude.md, "24h follow-up wiring". WATI's updateContactAttributes
        endpoint takes a customParams array, one {name, value} pair per
        attribute - the attribute itself must already exist as a defined
        Custom Attribute in WATI (Team Inbox -> any chat -> contact panel
        -> Edit -> +ADD NEW) before this call will have anywhere to write
        the value; it does not create new attribute definitions on the fly.
        """
        async with await self._client() as client:
            resp = await client.post(
                f"/api/v1/updateContactAttributes/{phone}",
                json={"customParams": [{"name": k, "value": v} for k, v in attributes.items()]},
            )
            resp.raise_for_status()
            return resp.json()

    @with_retry(attempts=2)
    async def assign_chat(self, phone: str, operator_email: str) -> dict:
        async with await self._client() as client:
            resp = await client.post(
                "/api/v1/assignOperator",
                json={"whatsappNumber": phone, "operatorEmail": operator_email},
            )
            resp.raise_for_status()
            return resp.json()


_client_singleton: WatiClient | None = None


def get_wati_client() -> WatiClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = WatiClient()
    return _client_singleton
