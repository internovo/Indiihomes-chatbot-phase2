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