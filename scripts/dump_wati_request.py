"""Diagnostic-only: prints the EXACT request send_template() will make
(method, URL, headers with the API key redacted, and body) without
actually sending it. For attaching to a WATI support ticket so they
can see precisely what's being called, rather than reconstructing it
from a description.

Usage:
    python scripts/dump_wati_request.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config import get_settings  # noqa: E402

settings = get_settings()

phone = "917208713112"
template_name = settings.wati_template_name
parameters = [{"name": "name", "value": "Yash Rane"}, {"name": "project_name", "value": "38 Avenue"}]

url = f"{settings.wati_endpoint.rstrip('/')}/api/v1/sendTemplateMessage?whatsappNumber={phone}"
headers = {
    "Content-Type": "application/json-patch+json",
    "Authorization": f"Bearer {settings.wati_api_key[:8]}...{settings.wati_api_key[-4:] if len(settings.wati_api_key) > 12 else '(short/empty)'}",
}
body = {"template_name": template_name, "broadcast_name": template_name, "parameters": parameters}

print("Method: POST")
print(f"URL: {url}")
print(f"Headers: {json.dumps(headers, indent=2)}")
print(f"Body: {json.dumps(body, indent=2)}")
print(f"\nWATI_ENDPOINT (raw from .env): {settings.wati_endpoint!r}")
print(f"WATI_API_KEY length: {len(settings.wati_api_key)} chars")
print("(Copy the above into the support ticket - API key intentionally truncated for safety, "
      "but its length is shown so WATI support can sanity-check nothing got truncated when you set it.)")
