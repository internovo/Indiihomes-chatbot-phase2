"""Pydantic request/response models for the campaign-facing HTTP API
(routes/campaign.py). Kept separate from models/campaign.py, which is
the internal CampaignRecord dataclass used by the worker/retry queue -
these models describe the wire format the WATI flow's webhook nodes
send and expect back.
"""
from typing import Optional

from pydantic import BaseModel, Field


class PropertyDetailRequest(BaseModel):
    """Body sent by the WATI flow's `camp_webhook-detail` node:
    {"phone": "@phone", "projectCode": "@project_code"}
    `projectCode` may be blank if the contact custom parameter was
    never set (e.g. an older contact, or the template's button was
    tapped before the campaign_worker had a chance to remember the
    mapping) - property_detail_service falls back to the phone-based
    registry lookup in that case."""
    phone: str
    project_code: Optional[str] = Field(default=None, alias="projectCode")

    model_config = {"populate_by_name": True, "extra": "ignore"}


class PropertyDetailResponse(BaseModel):
    """Flat, all-string response shape so every field maps directly
    onto a WATI Contact Custom Parameter via responseVariables without
    any type coercion surprises (WATI templates a `None` as the
    literal text "None", so every field defaults to "" instead)."""
    found: bool
    project_confirmed: str = "no"
    code: str = ""
    detail: str = ""
    location: str = ""
    price_range: str = ""
    configurations: str = ""
    carpet_area: str = ""
    possession_date: str = ""
    floor_plan_url: str = ""
    image_url: str = ""
