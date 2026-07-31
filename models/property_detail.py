"""Request/response models for the campaign-specific POST
/property-detail endpoint (routes/campaign.py).

Deliberately separate from models/property.py: that's the internal
Property model used during the polling cycle when a full Lead object
is on hand. This one is the wire format for the WATI flow's webhook
call, which only has a phone number (and maybe a project_code contact
attribute) to work with.
"""
from typing import Optional

from pydantic import BaseModel, Field


class PropertyDetailRequest(BaseModel):
    phone: str
    project_code: Optional[str] = Field(default=None, alias="projectCode")

    model_config = {"populate_by_name": True, "extra": "ignore"}


class PropertyDetailResponse(BaseModel):
    """All fields are strings on purpose, not bool/None - WATI Contact
    Custom Parameters and flow condition variables are text, and an
    empty string reads more predictably inside a WATI message than the
    literal word 'None' would."""

    found: str
    project_confirmed: str
    code: str = ""
    project_name: str = ""
    detail: str = ""
    location: str = ""
    price_range: str = ""
    configurations: str = ""
    carpet_area: str = ""
    possession_date: str = ""
    floor_plan_url: str = ""
    image_url: str = ""
