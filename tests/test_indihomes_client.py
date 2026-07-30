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
        return_value=Response(200, json={"leads": []})
    )
    client = IndihomesClient()
    await client.get_new_leads("2026-07-30T14:50:12.000Z")

    assert route.called
    request = route.calls.last.request
    assert request.url.params["afterDate"] == "2026-07-30T14:50:12.000Z"


@pytest.mark.asyncio
@respx.mock
async def test_get_new_leads_without_after_date_would_400_if_omitted():
    """Sanity check the other direction: a request missing afterDate
    is exactly the bug this fix addresses. Asserts the client never
    constructs that malformed request."""
    route = respx.get("https://api.indihomes.co.in/api/v1/get-new-leads").mock(
        side_effect=lambda request: Response(400) if "afterDate" not in request.url.params else Response(200, json={"leads": []})
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