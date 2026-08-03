"""Sends the REAL, approved Property Campaign template
(campaign_property_intro) to a real phone number via WATI - directly,
the same way send_test_generic_template.py does for the generic one.
Lets us reconfirm this specific template + parameter shape is still
working cleanly, independent of the campaign_generic_intro issue.

Deliberately does NOT call indihomes_client.update_lead() or
property_service - this is a pure WATI send test with a phone/name/
project_name you supply directly, not a full process_lead() run.

Parameter "name" fields are "1" and "2" (positional), matching the
approved template's {{1}}/{{2}} placeholders - see
services/formatter.py's template_parameters docstring for why.

Usage:
    python scripts/send_test_property_template.py --phone 9876543210 --name "Yash Rane" --project-name "38 Avenue"
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from config import get_settings  # noqa: E402
from integrations.wati_client import get_wati_client  # noqa: E402
from utils.helpers import normalize_phone  # noqa: E402


async def main(raw_phone: str, name: str, project_name: str):
    settings = get_settings()
    client = get_wati_client()
    template_name = settings.wati_template_name
    phone = normalize_phone(raw_phone)

    parameters = [{"name": "1", "value": name}, {"name": "2", "value": project_name}]
    print(f"Sending template {template_name!r} to {phone} (normalized from {raw_phone!r})")
    print(f"Parameters: {parameters}")
    try:
        result = await client.send_template(phone, template_name, parameters)
    except httpx.HTTPStatusError as exc:
        print(f"!! WATI returned {exc.response.status_code}")
        print(f"!! Response body: {exc.response.text}")
        return
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic script
        print(f"!! send_template raised: {exc}")
        return

    print("WATI response:")
    print(result)
    print(f"\nCheck {phone}'s WhatsApp to confirm delivery.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phone", required=True)
    parser.add_argument("--name", default="Test")
    parser.add_argument("--project-name", default="38 Avenue")
    args = parser.parse_args()
    asyncio.run(main(args.phone, args.name, args.project_name))
