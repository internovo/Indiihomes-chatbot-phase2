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
import httpx

from config import get_settings
from utils.logger import get_logger
from utils.retry import with_retry

logger = get_logger("wati_client")


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
            resp.raise_for_status()
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
