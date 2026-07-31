"""Resolves which property a campaign lead is asking about, and turns
the backend's raw JSON into the Property model the rest of the
pipeline works with.

raw_to_property() is the single source of truth for that JSON->Property
mapping - used here (resolve_property, the polling-cycle path with a
full Lead on hand) AND by campaign_property_service.py (the
/property-detail webhook path, phone-only). They used to duplicate
this mapping independently, which is how a field-name mismatch against
the REAL backend shape went unnoticed in one path but not the other -
see the field notes below, confirmed against live
/fetchPaginatedFilteredProjectList data on 31 Jul 2026.
"""
from typing import Any

from integrations.indihomes_client import IndihomesClient
from models.lead import Lead
from models.property import Property
from services.formatter import normalize_media_urls
from utils.helpers import format_date
from utils.logger import get_logger

logger = get_logger("property_service")


def _first_present(raw: dict, *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value is not None and value != "":
            return value
    return None


def _price_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        # Real shape: {"value": 218, "unit": "Lakh"} - not a plain number/string.
        amount, unit = value.get("value"), value.get("unit", "")
        if amount in (None, ""):
            return None
        return f"{amount} {unit}".strip()
    return str(value)


def _location_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        # Real shape: {"label": "Goregaon West", "value": "goregaon west"}
        return value.get("label") or value.get("value") or None
    return str(value)


def _configurations_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        # Real shape: flatConfiguration -> ["2BHK", "3BHK", "Jodi"]
        joined = ", ".join(str(v) for v in value if v)
        return joined or None
    return str(value)


def _carpet_area_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        # Real shape: carpetSize -> {"min": 690, "max": 903, "unit": "Sq. Ft."}
        lo, hi, unit = value.get("min"), value.get("max"), value.get("unit", "")
        if lo and hi and str(lo) != str(hi):
            return f"{lo}-{hi} {unit}".strip()
        single = lo or hi
        return f"{single} {unit}".strip() if single else None
    return str(value)


def _floor_plan_url(raw: dict) -> str | None:
    direct = _first_present(raw, "floorPlanUrl", "floor_plan_url")
    if direct:
        return direct
    floor_urls = raw.get("floor_urls")
    if isinstance(floor_urls, list) and floor_urls:
        return floor_urls[0]
    inventory = raw.get("flatInventory")
    if isinstance(inventory, list) and inventory:
        for item in inventory:
            if isinstance(item, dict) and item.get("floorPlanUrl"):
                return item["floorPlanUrl"]
    return None


def raw_to_property(raw: dict, fallback_code: str = "", fallback_name: str = "") -> Property:
    """Turns the backend's raw project JSON (from /fetchProject,
    /fetchProjectByName, or /fetchPaginatedFilteredProjectList) into a
    Property. Every helper above is defensive about the real nested
    shapes (dicts/lists) the live backend actually returns, while still
    accepting plain strings/numbers - so older/simpler fixtures and
    possible future flatter API responses both still work."""
    media_urls = normalize_media_urls(_first_present(raw, "media_urls", "mediaUrls"))
    image_url = _first_present(raw, "imageUrl", "image_url") or (media_urls[0] if media_urls else None)

    return Property(
        project_code=_first_present(raw, "projectCode", "project_code", "projectName") or fallback_code or "",
        project_name=_first_present(raw, "displayName", "display_name", "projectName", "project_name") or fallback_name or "",
        location=_location_text(_first_present(raw, "location")),
        price_range=_price_text(_first_present(raw, "startingPrice", "starting_price", "priceRange", "price_range")),
        configurations=_configurations_text(_first_present(raw, "flatConfiguration", "configurations")),
        possession_date=format_date(_first_present(raw, "possessionStartDate", "possessionDate", "possession_date")),
        carpet_area=_carpet_area_text(_first_present(raw, "carpetSize", "carpetArea", "carpet_area")),
        floor_plan_url=_floor_plan_url(raw),
        image_url=image_url,
        media_urls=media_urls,
    )


async def resolve_property(client: IndihomesClient, lead: Lead) -> Property | None:
    """Prefers an explicit project_code (the reliable path the plan
    recommends every campaign eventually attach). Falls back to
    project_name / fetchProjectByName for campaigns that only pass a
    name today."""
    raw: dict | None = None

    if lead.project_code:
        raw = await client.fetch_project(lead.project_code)

    if raw is None and lead.project_name:
        raw = await client.fetch_project_by_name(lead.project_name)

    if raw is None:
        logger.warning(
            "Could not resolve a property for lead %s (project_code=%s, project_name=%s)",
            lead.id, lead.project_code, lead.project_name,
        )
        return None

    return raw_to_property(raw, fallback_code=lead.project_code or "", fallback_name=lead.project_name or "")
