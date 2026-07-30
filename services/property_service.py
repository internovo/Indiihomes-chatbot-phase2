"""Resolves which property a campaign lead is asking about, and turns
the backend's raw JSON into the Property model the rest of the
pipeline works with."""
from typing import Any

from integrations.indihomes_client import IndihomesClient
from models.lead import Lead
from models.property import Property
from services.formatter import normalize_media_urls
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
    return str(value)


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

    media_urls = normalize_media_urls(_first_present(raw, "media_urls", "mediaUrls"))
    image_url = _first_present(raw, "imageUrl", "image_url") or (media_urls[0] if media_urls else None)

    return Property(
        project_code=_first_present(raw, "projectCode", "project_code", "projectName") or lead.project_code or "",
        project_name=_first_present(raw, "displayName", "display_name", "projectName", "project_name") or lead.project_name or "",
        location=_first_present(raw, "location"),
        price_range=_price_text(_first_present(raw, "startingPrice", "starting_price", "priceRange", "price_range")),
        configurations=_first_present(raw, "configurations"),
        possession_date=_first_present(raw, "possessionDate", "possession_date"),
        carpet_area=_first_present(raw, "carpetArea", "carpet_area"),
        floor_plan_url=_first_present(raw, "floorPlanUrl", "floor_plan_url"),
        image_url=image_url,
        media_urls=media_urls,
    )