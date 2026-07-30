"""Pydantic model representing a CRM lead, as returned by
GET /get-new-leads on the existing IndiHomes backend."""
from typing import Optional

from pydantic import BaseModel, Field


class Lead(BaseModel):
    id: str = Field(alias="_id")
    name: Optional[str] = ""
    phone: str
    lead_source: str = ""
    project_code: Optional[str] = Field(default=None, alias="projectCode")
    project_name: Optional[str] = Field(default=None, alias="projectName")
    status: Optional[str] = None
    # Used by campaign_worker/utils.checkpoint to track how far the
    # checkpoint should advance. Backend may call this either field.
    lead_date: Optional[str] = Field(default=None, alias="leadDate")
    created_at: Optional[str] = Field(default=None, alias="createdAt")

    model_config = {"populate_by_name": True, "extra": "ignore"}

    @property
    def timestamp(self) -> Optional[str]:
        """Best available timestamp for checkpoint purposes."""
        return self.lead_date or self.created_at
