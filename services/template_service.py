"""Builds the WATI template payload for a resolved (lead, property)
pair. Kept separate from campaign_service so the payload shape can be
unit tested without needing a live WATI client."""
from config import get_settings
from models.lead import Lead
from models.property import Property
from services.formatter import template_parameters


def build_template_payload(lead: Lead, prop: Property) -> dict:
    settings = get_settings()
    return {
        "phone": lead.phone,
        "template_name": settings.wati_template_name,
        "parameters": template_parameters(lead.name, prop),
    }
