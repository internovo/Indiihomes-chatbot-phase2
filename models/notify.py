"""Request/response models for POST /notify-advisor.

Fired by the WATI campaign flow when a lead clicks "Book a Site Visit"
or "Talk to an Advisor" - replaces the old save-lead CRM write for
those two branches per the team's decision to notify the advisor by
email instead of writing to the CRM.
"""
from typing import Optional

from pydantic import BaseModel


class NotifyAdvisorRequest(BaseModel):
    phone: str
    name: str = ""
    project_code: Optional[str] = None
    project_name: Optional[str] = None
    reason: str  # "advisor_requested" | "site_visit_no_slots" | "site_visit_booked"
    slot_label: Optional[str] = None
    advisor: Optional[str] = None

    model_config = {"extra": "ignore"}


class NotifyAdvisorResponse(BaseModel):
    """String on purpose, not bool - same convention as
    PropertyDetailResponse: WATI flow variables are text."""
    sent: str
