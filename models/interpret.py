"""Request/response models for the campaign flow's free-text fallback,
POST /interpret-message (routes/campaign.py).

Sibling of models/property_detail.py - same convention (string fields
throughout, not bool/None, since WATI Contact Custom Parameters and
flow condition variables are text). See services/campaign_intent_router.py
for the classification logic and, importantly, the honesty note there
about these intents being inferred rather than grounded in a captured
transcript.
"""
from typing import Optional

from pydantic import BaseModel, Field


class InterpretMessageRequest(BaseModel):
    phone: str
    message: str = ""
    name: Optional[str] = ""
    project_code: Optional[str] = Field(default=None, alias="projectCode")
    project_name: Optional[str] = Field(default=None, alias="projectName")

    model_config = {"populate_by_name": True, "extra": "ignore"}


class InterpretMessageResponse(BaseModel):
    intent: str = "none"
    is_global: str = "no"
    handled: str = "no"
    reply_text: str = ""
