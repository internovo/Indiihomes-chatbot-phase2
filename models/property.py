"""Pydantic model for property/project responses coming back from
/fetchProject or /fetchProjectByName."""
from typing import Optional

from pydantic import BaseModel, Field


class Property(BaseModel):
    project_code: str
    project_name: str
    location: Optional[str] = None
    price_range: Optional[str] = None
    configurations: Optional[str] = None
    possession_date: Optional[str] = None
    carpet_area: Optional[str] = None
    floor_plan_url: Optional[str] = None
    image_url: Optional[str] = None
    media_urls: list[str] = Field(default_factory=list)

    model_config = {"extra": "ignore"}