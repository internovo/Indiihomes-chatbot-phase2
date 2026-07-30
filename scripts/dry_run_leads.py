"""Safe, read-only dry run against the REAL IndiHomes backend
(uses whatever INDIHOMES_BASE_URL / INDIHOMES_API_KEY are in your
.env). Fetches leads since the current checkpoint, shows how each one
classifies and whether its property resolves - but sends NO WhatsApp
template, updates NO lead status, and does NOT advance the checkpoint.

Use this to sanity-check the afterDate fix and the fetchProject `id`
fix against real data before trusting the live worker with it.

Usage:
    python scripts/dry_run_leads.py
    python scripts/dry_run_leads.py --after 2026-07-29T00:00:00.000Z
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.indihomes_client import get_indihomes_client  # noqa: E402
from services import lead_service, property_service  # noqa: E402
from utils import checkpoint  # noqa: E402


async def main(after_date: str | None):
    client = get_indihomes_client()
    after_date = after_date or checkpoint.get_after_date()

    print(f"Fetching leads with afterDate={after_date} ...")
    raw_leads = await client.get_new_leads(after_date)
    print(f"Backend returned {len(raw_leads)} lead(s).\n")

    leads = lead_service.parse_leads(raw_leads)
    campaign_leads = lead_service.filter_campaign_leads(leads)

    print(f"{len(leads)} parsed OK, {len(campaign_leads)} classified as campaign/portal.\n")

    for lead in campaign_leads:
        print(f"--- Lead {lead.id} ({lead.phone}) ---")
        print(f"  source: {lead.lead_source!r}")
        print(f"  project_code: {lead.project_code!r}  project_name: {lead.project_name!r}")
        try:
            prop = await property_service.resolve_property(client, lead)
        except Exception as exc:  # noqa: BLE001 - this is a diagnostic script
            print(f"  !! property lookup raised: {exc}")
            continue
        if prop is None:
            print("  !! property did NOT resolve - this lead would go to retry_worker in production")
        else:
            print(f"  OK -> resolved to '{prop.project_name}' ({prop.project_code})")
        print()

    print("Dry run complete - no messages sent, no lead status changed, checkpoint untouched.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--after", dest="after_date", default=None, help="Override afterDate (ISO timestamp)")
    args = parser.parse_args()
    asyncio.run(main(args.after_date))
