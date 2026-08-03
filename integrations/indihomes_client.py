"""Thin async wrapper around the existing IndiHomes backend REST API.
No business logic lives here on purpose - just HTTP calls, so that
services/ can be unit tested against a mocked client instead of a
mocked HTTP layer."""
import httpx

from config import get_settings
from utils.logger import get_logger
from utils.retry import with_retry

logger = get_logger("indihomes_client")


class IndihomesClient:
    def __init__(self):
        settings = get_settings()
        self._base_url = settings.indihomes_base_url.rstrip("/")
        self._headers = {"Content-Type": "application/json"}
        if settings.indihomes_api_key:
            self._headers["Authorization"] = f"Bearer {settings.indihomes_api_key}"
        self._timeout = settings.http_timeout_seconds

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self._base_url, headers=self._headers, timeout=self._timeout)

    @with_retry(attempts=3)
    async def get_new_leads(self, after_date: str) -> list[dict]:
        """`afterDate` is required by the backend - calling this
        without it returns a 400. Callers should get the value from
        utils.checkpoint.get_after_date(), not construct it themselves.

        Real response shape (confirmed 3 Aug 2026 via a raw dump,
        after this had been silently returning [] for the entire
        project's life): {"success": true, "data": [...], "count": N}.
        Previously looked for a "leads" key, which never existed -
        every real lead (294 of them, at the time this was found) was
        being silently discarded despite every request returning a
        clean 200 OK. This is exactly the kind of failure that looks
        completely healthy in logs while doing nothing - no exception,
        no error status, just an empty result every time.
        """
        async with await self._client() as client:
            resp = await client.get("/get-new-leads", params={"afterDate": after_date})
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and data.get("success") is False:
                logger.warning("get-new-leads responded success=false: %s", data)
            return data.get("data", data if isinstance(data, list) else [])

    @with_retry(attempts=3)
    async def fetch_project_by_name(self, project_name: str) -> dict | None:
        async with await self._client() as client:
            resp = await client.get("/fetchProjectByName", params={"projectName": project_name})
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()

    @with_retry(attempts=3)
    async def fetch_project(self, project_id: str) -> dict | None:
        """The backend expects the project's `id` field, NOT
        `projectCode` - despite the field being called project_code
        elsewhere in this codebase (Lead.project_code / the plan doc's
        terminology). Whatever value is stored there is what gets sent
        as `id` here."""
        async with await self._client() as client:
            resp = await client.get("/fetchProject", params={"id": project_id})
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()

    @with_retry(attempts=2)
    async def fetch_filtered_projects(self, filters: dict) -> list[dict]:
        async with await self._client() as client:
            resp = await client.get("/fetchPaginatedFilteredProjectList", params=filters)
            resp.raise_for_status()
            data = resp.json()
            return data.get("projects", data if isinstance(data, list) else [])

    @with_retry(attempts=3)
    async def available_slots(self, project_code: str) -> list[dict]:
        async with await self._client() as client:
            resp = await client.post("/available-slots", json={"projectCode": project_code})
            resp.raise_for_status()
            data = resp.json()
            return data.get("slots", data if isinstance(data, list) else [])

    @with_retry(attempts=3)
    async def book_slot(self, lead_id: str, slot_id: str) -> dict:
        async with await self._client() as client:
            resp = await client.post("/book-slot", json={"leadId": lead_id, "slotId": slot_id})
            resp.raise_for_status()
            return resp.json()

    @with_retry(attempts=3)
    async def update_lead(self, lead_id: str, payload: dict) -> dict:
        async with await self._client() as client:
            resp = await client.patch(f"/update-leads-by-id/{lead_id}", json=payload)
            resp.raise_for_status()
            return resp.json()


_client_singleton: IndihomesClient | None = None


def get_indihomes_client() -> IndihomesClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = IndihomesClient()
    return _client_singleton
