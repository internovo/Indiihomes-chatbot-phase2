"""Sends the REAL, approved Generic Interest template to a real phone
number via WATI - the one thing about Path C never actually tested
against the live WATI API (everything else has only exercised
campaign_service.process_generic_lead against fakes).

Deliberately does NOT call indihomes_client.update_lead() - this is a
pure send test, not a full process_generic_lead() run, so it never
touches a real CRM record.

Parameter "name" is "1" (positional), matching the approved template's
{{1}} placeholder - see services/campaign_service.py's
process_generic_lead for why.

After running this, check the target phone's WhatsApp for the
message, then tap its Yes/Interested button for real and confirm the
keyword trigger correctly launches Indihomes_Generic_Interest_Assistant_v1
- that closes the loop on Path C the same way "View Details" did for
Path A.

Usage:
    python scripts/send_test_generic_template.py --phone 9876543210 --name Priya
    python scripts/send_test_generic_template.py --phone 9876543210 --name "Yash Rane"   (quote multi-word names)
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


async def main(raw_phone: str, name: str):
    settings = get_settings()
    client = get_wati_client()
    template_name = settings.wati_generic_template_name
    phone = normalize_phone(raw_phone)

    print(f"Sending template {template_name!r} to {phone} (normalized from {raw_phone!r}), name param: {name!r} ...")
    try:
        result = await client.send_template(phone, template_name, [{"name": "1", "value": name}])
    except httpx.HTTPStatusError as exc:
        print(f"!! WATI returned {exc.response.status_code}")
        print(f"!! Response body: {exc.response.text}")
        return
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic script
        print(f"!! send_template raised: {exc}")
        return

    print("WATI response:")
    print(result)
    print(f"\nNow check {phone}'s WhatsApp, tap the Yes/Interested button, and confirm the "
          "keyword trigger launches the Generic Interest flow correctly.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--phone", required=True, help="Real phone number to send to - with or without 91 prefix")
    parser.add_argument("--name", default="Test", help="Value for the template's {{1}} name parameter - quote if it has a space")
    args = parser.parse_args()
    asyncio.run(main(args.phone, args.name))
