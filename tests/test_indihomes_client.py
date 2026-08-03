"""Tests that integrations/indihomes_client.py builds the exact
requests the backend expects - the whole point of this file existing
is to catch API contract regressions before they hit production.

Uses respx to intercept httpx calls - no real network access.
"""
import pytest
import respx
from httpx import Response

from integrations.indihomes_client import IndihomesClient


@pytest.mark.asyncio
@respx.mock
async def test_get_new_leads_sends_after_date_param():
    route = respx.get("https://api.indihomes.co.in/api/v1/get-new-leads").mock(
        return_value=Response(200, json={"success": True, "data": [], "count": 0})
    )
    client = IndihomesClient()
    await client.get_new_leads("2026-07-30T14:50:12.000Z")

    assert route.called
    request = route.calls.last.request
    assert request.url.params["afterDate"] == "2026-07-30T14:50:12.000Z"


@pytest.mark.asyncio
@respx.mock
async def test_get_new_leads_extracts_the_data_key_not_leads():
    """The actual 3 Aug 2026 bug: the real backend response is
    {"success": true, "data": [...], "count": N} - there is no "leads"
    key at all. A previous version of this client looked for "leads"
    and silently returned [] on every single real call for the
    project's entire life, despite every request returning a clean
    200 OK. This test uses a non-empty, realistic payload specifically
    so an empty-list assertion can't accidentally pass either way."""
    route = respx.get("https://api.indihomes.co.in/api/v1/get-new-leads").mock(
        return_value=Response(
            200,
            json={
                "success": True,
                "count": 2,
                "data": [
                    {"id": "housing_abc123", "name": "Mallika Parikh", "phone": "+918850184688",
                     "leadSource": "Housing.com", "projectName": "Ariha Opulence", "leadDate": "2026-08-03T09:58:00.000Z"},
                    {"id": "2852824985059416", "name": "Vijay Thakkar", "phone": "+918104065655",
                     "leadSource": "Ethics Orovia EOI Malad W Video v1 3007", "leadDate": "2026-08-03T09:07:30.997Z"},
                ],
            },
        )
    )
    client = IndihomesClient()
    result = await client.get_new_leads("2026-07-01T00:00:00.000Z")

    assert len(result) == 2
    assert result[0]["name"] == "Mallika Parikh"
    assert result[1]["leadSource"] == "Ethics Orovia EOI Malad W Video v1 3007"


@pytest.mark.asyncio
@respx.mock
async def test_get_new_leads_without_after_date_would_400_if_omitted():
    """Sanity check the other direction: a request missing afterDate
    is exactly the bug this fix addresses. Asserts the client never
    constructs that malformed request."""
    route = respx.get("https://api.indihomes.co.in/api/v1/get-new-leads").mock(
        side_effect=lambda request: Response(400) if "afterDate" not in request.url.params else Response(200, json={"success": True, "data": [], "count": 0})
    )
    client = IndihomesClient()
    await client.get_new_leads("2026-07-30T14:50:12.000Z")
    assert route.calls.last.response.status_code == 200


@pytest.mark.asyncio
@respx.mock
async def test_fetch_project_sends_id_as_query_param():
    route = respx.get("https://api.indihomes.co.in/api/v1/fetchProject").mock(
        return_value=Response(200, json={"projectName": "INV_GW_552", "displayName": "38 Avenue"})
    )
    client = IndihomesClient()
    await client.fetch_project("INV_GW_552")

    assert route.called
    request = route.calls.last.request
    assert request.url.params["id"] == "INV_GW_552"
    assert request.content == b""


@pytest.mark.asyncio
@respx.mock
async def test_fetch_project_by_name_sends_project_name_as_query_param():
    route = respx.get("https://api.indihomes.co.in/api/v1/fetchProjectByName").mock(
        return_value=Response(200, json={"projectName": "INV_GW_552", "displayName": "38 Avenue"})
    )
    client = IndihomesClient()
    await client.fetch_project_by_name("38 Avenue")

    assert route.called
    request = route.calls.last.request
    assert request.url.params["projectName"] == "38 Avenue"
    assert request.content == b""


@pytest.mark.asyncio
@respx.mock
async def test_fetch_filtered_projects_sends_filters_as_query_params_and_returns_projects():
    route = respx.get("https://api.indihomes.co.in/api/v1/fetchPaginatedFilteredProjectList").mock(
        return_value=Response(
            200,
            json={
                "success": True,
                "totalItems": 1,
                "currentPage": 1,
                "totalPages": 1,
                "projects": [{"displayName": "38 Avenue"}],
                "fromCache": False,
            },
        )
    )
    client = IndihomesClient()
    result = await client.fetch_filtered_projects({"area": "Gurgaon", "limit": 5, "sortBy": "price"})

    assert result == [{"displayName": "38 Avenue"}]
    request = route.calls.last.request
    assert request.url.params["area"] == "Gurgaon"
    assert request.url.params["limit"] == "5"
    assert request.url.params["sortBy"] == "price"
    assert request.content == b""


@pytest.mark.asyncio
@respx.mock
async def test_fetch_project_returns_none_on_404():
    respx.get("https://api.indihomes.co.in/api/v1/fetchProject").mock(return_value=Response(404))
    client = IndihomesClient()
    result = await client.fetch_project("does-not-exist")
    assert result is None
