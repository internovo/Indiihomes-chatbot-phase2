"""Tests for the /property-detail webhook's resolution logic.

Covers two related fixes:
- 31 Jul 2026: campaign_context/the WATI flow only ever have a
  projectCode/projectName-style value, never the backend's internal
  `id`, but resolve_campaign_property() used to call ONLY
  fetch_project(code) (which needs `id`) with no fallback - so it
  would silently return "not found" for every real lead.
- 3 Aug 2026: even the name fallback can miss a real project if its
  stored displayName has a data-entry typo (e.g. extra whitespace) -
  resolve_raw_project's third tier (a verified, first-word searchText
  fallback) covers that case too. Both fixes are shared with
  property_service.py via resolve_raw_project/raw_to_property, not
  duplicated here.
"""
import pytest

from services import campaign_context, campaign_property_service


class FakeIndihomesClient:
    """Stand-in for integrations.indihomes_client.IndihomesClient.
    by_id simulates the backend's real /fetchProject (keyed by its
    internal `id`, e.g. "BII0kGNfkFaU"); by_name simulates
    /fetchProjectByName (keyed by projectName/displayName, e.g.
    "INV_GW_552"); by_search simulates /fetchPaginatedFilteredProjectList's
    searchText fallback (keyed by search string - note this is now the
    FIRST WORD of the sought name, not the full name - returns a list)."""

    def __init__(self, by_id=None, by_name=None, by_search=None):
        self._by_id = by_id or {}
        self._by_name = by_name or {}
        self._by_search = by_search or {}

    async def fetch_project(self, project_id):
        return self._by_id.get(project_id)

    async def fetch_project_by_name(self, project_name):
        return self._by_name.get(project_name)

    async def fetch_filtered_projects(self, filters):
        return self._by_search.get(filters.get("searchText"), [])


RAW_PROJECT = {
    "id": "BII0kGNfkFaU",
    "projectName": "INV_GW_552",
    "displayName": "38 Avenue",
    "location": {"label": "Goregaon West", "value": "goregaon west"},
}


@pytest.fixture(autouse=True)
def _clear_campaign_context():
    campaign_context.clear()
    yield
    campaign_context.clear()


@pytest.mark.asyncio
async def test_resolves_directly_when_code_is_actually_a_real_id():
    client = FakeIndihomesClient(by_id={"BII0kGNfkFaU": RAW_PROJECT})
    result = await campaign_property_service.resolve_campaign_property(client, "919876543210", "BII0kGNfkFaU")
    assert result.found == "yes"
    assert result.project_name == "38 Avenue"


@pytest.mark.asyncio
async def test_falls_back_to_name_lookup_when_code_is_not_a_real_id():
    """The realistic case: campaign_context/the flow only ever hand us
    a projectName/projectCode-style value (e.g. "INV_GW_552"), not the
    backend's `id`. fetch_project("INV_GW_552") correctly finds
    nothing; the fix is falling back to fetch_project_by_name."""
    client = FakeIndihomesClient(by_name={"INV_GW_552": RAW_PROJECT})
    result = await campaign_property_service.resolve_campaign_property(client, "919876543210", "INV_GW_552")
    assert result.found == "yes"
    assert result.project_name == "38 Avenue"
    assert result.location == "Goregaon West"


@pytest.mark.asyncio
async def test_falls_back_to_fuzzy_search_when_exact_name_has_a_data_typo():
    """Mirrors the real "Ariha Opulence" finding: the exact-match name
    lookup misses because the backend's stored displayName has a typo
    (extra whitespace) the search endpoint's literal substring match
    also can't bridge on the full phrase - so this searches the first
    word ("Ariha") and verifies against a decoy ("Ariha Vincere",
    sharing that first word) to confirm the right one gets picked."""
    client = FakeIndihomesClient(by_search={
        "Ariha": [
            {"id": "OTHER123", "projectName": "INV_GW_550", "displayName": "Ariha Vincere"},
            {"id": "Zi8e9PXdVluD", "projectName": "INV_GW_551", "displayName": "Ariha  Opulence "},
        ],
    })
    result = await campaign_property_service.resolve_campaign_property(client, "919876543210", "Ariha Opulence")
    assert result.found == "yes"
    assert result.code == "INV_GW_551"


@pytest.mark.asyncio
async def test_falls_back_to_campaign_context_when_no_project_code_in_request():
    """Mirrors the real WATI flow: the /property-detail request body's
    projectCode is a dead flow variable (nothing sets it), so the
    lookup depends entirely on campaign_context, populated when the
    template was sent."""
    campaign_context.remember("919876543210", "INV_GW_552")
    client = FakeIndihomesClient(by_name={"INV_GW_552": RAW_PROJECT})

    result = await campaign_property_service.resolve_campaign_property(client, "919876543210", None)

    assert result.found == "yes"
    assert result.project_name == "38 Avenue"


@pytest.mark.asyncio
async def test_returns_not_found_when_nothing_resolves():
    client = FakeIndihomesClient()
    result = await campaign_property_service.resolve_campaign_property(client, "919876543210", "does-not-exist")
    assert result.found == "no"
    assert result.project_confirmed == "no"


@pytest.mark.asyncio
async def test_returns_not_found_when_no_code_available_anywhere():
    client = FakeIndihomesClient()
    result = await campaign_property_service.resolve_campaign_property(client, "919876543210", None)
    assert result.found == "no"

@pytest.mark.asyncio
@pytest.mark.parametrize("incoming_project_code", ["", "   ", "@project_code", "{{project_code}}"])
async def test_falls_back_to_campaign_context_when_project_code_is_unavailable_or_placeholder(incoming_project_code):
    campaign_context.remember("919876543210", "INV_GW_552")
    client = FakeIndihomesClient(by_name={"INV_GW_552": RAW_PROJECT})

    result = await campaign_property_service.resolve_campaign_property(
        client, "919876543210", incoming_project_code,
    )

    assert result.found == "yes"
    assert result.code == "INV_GW_552"
    assert result.project_name == "38 Avenue"


@pytest.mark.asyncio
@pytest.mark.parametrize("incoming_project_code", ["@project_code", "{{project_code}}"])
async def test_unresolved_placeholder_without_context_returns_not_found(incoming_project_code):
    client = FakeIndihomesClient()

    result = await campaign_property_service.resolve_campaign_property(
        client, "919876543210", incoming_project_code,
    )

    assert result.found == "no"
    assert result.project_confirmed == "no"
