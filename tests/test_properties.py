"""Tests for property resolution - project_code path preferred, falls
back to project_name lookup, and formatting into the Property model."""
import pytest

from models.lead import Lead
from models.property import Property
from services import formatter, property_service


class FakeIndihomesClient:
    """Stand-in for integrations.indihomes_client.IndihomesClient so
    these tests don't touch HTTP at all."""

    def __init__(self, by_code=None, by_name=None):
        self._by_code = by_code or {}
        self._by_name = by_name or {}

    async def fetch_project(self, project_code):
        return self._by_code.get(project_code)

    async def fetch_project_by_name(self, project_name):
        return self._by_name.get(project_name)


@pytest.mark.asyncio
async def test_resolve_property_prefers_project_code():
    client = FakeIndihomesClient(
        by_code={"ETH-ORO-01": {"projectCode": "ETH-ORO-01", "projectName": "ETH-ORO-01", "displayName": "Ethics Orovia"}},
    )
    lead = Lead(_id="1", phone="919876543210", lead_source="Housing.com", projectCode="ETH-ORO-01")
    prop = await property_service.resolve_property(client, lead)
    assert prop is not None
    assert prop.project_code == "ETH-ORO-01"
    assert prop.project_name == "Ethics Orovia"


@pytest.mark.asyncio
async def test_resolve_property_falls_back_to_name():
    client = FakeIndihomesClient(
        by_name={"Ethics Orovia": {"projectCode": "ETH-ORO-01", "projectName": "ETH-ORO-01", "displayName": "Ethics Orovia"}},
    )
    lead = Lead(_id="1", phone="919876543210", lead_source="Housing.com", projectName="Ethics Orovia")
    prop = await property_service.resolve_property(client, lead)
    assert prop is not None
    assert prop.project_code == "ETH-ORO-01"


@pytest.mark.asyncio
async def test_resolve_property_maps_actual_api_fields():
    """Older/flatter fixture shape (plain number price, no nested
    location/carpet/config objects) - kept passing on purpose to prove
    raw_to_property still tolerates non-nested values, not just the
    real shape covered below."""
    client = FakeIndihomesClient(
        by_code={
            "INV_GW_552": {
                "projectName": "INV_GW_552",
                "displayName": "38 Avenue",
                "startingPrice": 4500000,
                "media_urls": [
                    "https://cdn.example.com/plain.jpg",
                    {"url": "https://cdn.example.com/elevation.jpg", "tag": "Elevation"},
                ],
            }
        },
    )
    lead = Lead(_id="1", phone="919876543210", lead_source="Housing.com", projectCode="INV_GW_552")

    prop = await property_service.resolve_property(client, lead)

    assert prop is not None
    assert prop.project_code == "INV_GW_552"
    assert prop.project_name == "38 Avenue"
    assert prop.price_range == "4500000"
    assert prop.media_urls == ["https://cdn.example.com/plain.jpg", "https://cdn.example.com/elevation.jpg"]
    assert prop.image_url == "https://cdn.example.com/plain.jpg"


@pytest.mark.asyncio
async def test_resolve_property_maps_real_nested_backend_shape():
    """Locks in the actual /fetchPaginatedFilteredProjectList response
    shape confirmed against live data on 31 Jul 2026 - location and
    startingPrice and carpetSize are nested objects, configurations
    comes from flatConfiguration (a list), and possession is
    possessionStartDate (month precision, e.g. "2028-01"). A previous
    version of this mapping assumed flat string/number fields for all
    of these and would have silently produced blank or garbled values
    for every one of them against real data."""
    client = FakeIndihomesClient(
        by_code={
            "BII0kGNfkFaU": {
                "id": "BII0kGNfkFaU",
                "projectName": "INV_GW_552",
                "displayName": "38 Avenue",
                "location": {"label": "Goregaon West", "value": "goregaon west"},
                "startingPrice": {"value": 218, "unit": "Lakh"},
                "carpetSize": {"min": 690, "max": 903, "unit": "Sq. Ft."},
                "flatConfiguration": ["2BHK", "3BHK", "Jodi"],
                "possessionStartDate": "2028-01",
                "floor_urls": ["https://cdn.example.com/floorplan.png"],
                "media_urls": [{"url": "https://cdn.example.com/lobby.jpg", "tag": "Lobby"}],
            }
        },
    )
    lead = Lead(_id="1", phone="919876543210", lead_source="Housing.com", projectCode="BII0kGNfkFaU")

    prop = await property_service.resolve_property(client, lead)

    assert prop is not None
    assert prop.project_name == "38 Avenue"
    assert prop.location == "Goregaon West"
    assert prop.price_range == "218 Lakh"
    assert prop.carpet_area == "690-903 Sq. Ft."
    assert prop.configurations == "2BHK, 3BHK, Jodi"
    assert prop.possession_date == "Jan 2028"
    assert prop.floor_plan_url == "https://cdn.example.com/floorplan.png"
    assert prop.image_url == "https://cdn.example.com/lobby.jpg"


@pytest.mark.asyncio
async def test_resolve_property_returns_none_when_unresolvable():
    client = FakeIndihomesClient()
    lead = Lead(_id="1", phone="919876543210", lead_source="Housing.com")
    prop = await property_service.resolve_property(client, lead)
    assert prop is None


def test_formatter_builds_readable_card():
    prop = Property(project_code="ETH-ORO-01", project_name="Ethics Orovia", location="Malad West")
    card = formatter.property_to_whatsapp_card(prop)
    assert "Ethics Orovia" in card
    assert "Malad West" in card


def test_formatter_normalizes_media_urls():
    assert formatter.normalize_media_urls([
        "https://cdn.example.com/plain.jpg",
        {"url": "https://cdn.example.com/elevation.jpg", "tag": "Elevation"},
        {"tag": "Missing URL"},
        None,
    ]) == ["https://cdn.example.com/plain.jpg", "https://cdn.example.com/elevation.jpg"]
