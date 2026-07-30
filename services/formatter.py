"""Converts backend property JSON/models into user-friendly WhatsApp
text. No API calls happen here - pure formatting so it stays trivial
to unit test."""
from typing import Any

from models.property import Property
from utils.helpers import format_date


def normalize_media_urls(media_urls: Any) -> list[str]:
    """Accepts both API media formats and returns plain URL strings."""
    if not isinstance(media_urls, list):
        return []

    normalized: list[str] = []
    for media in media_urls:
        if isinstance(media, str) and media:
            normalized.append(media)
        elif isinstance(media, dict):
            url = media.get("url")
            if isinstance(url, str) and url:
                normalized.append(url)
    return normalized


def property_to_whatsapp_card(prop: Property) -> str:
    lines = [f"*{prop.project_name}*"]
    if prop.location:
        lines.append(f"\U0001F4CD {prop.location}")
    if prop.configurations:
        lines.append(f"\U0001F3E0 {prop.configurations}")
    if prop.carpet_area:
        lines.append(f"\U0001F4D0 Carpet area: {prop.carpet_area}")
    if prop.price_range:
        lines.append(f"\U0001F4B0 {prop.price_range}")
    if prop.possession_date:
        lines.append(f"\U0001F4C5 Possession: {format_date(prop.possession_date)}")
    return "\n".join(lines)


def template_parameters(lead_name: str, prop: Property) -> list[dict]:
    """Shape WATI expects for template variable substitution."""
    return [
        {"name": "name", "value": lead_name or "there"},
        {"name": "project_name", "value": prop.project_name},
    ]