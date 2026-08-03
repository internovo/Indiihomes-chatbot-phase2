"""Manually reprocesses ONE specific lead through the real
process_lead()/process_generic_lead() pipeline, identified by its CRM
_id - bypassing the checkpoint and retry queue entirely.

Built for exactly this situation: a lead failed processing due to a
bug that's since been fixed, but a redeploy in between wiped the
in-memory retry queue (see DOCUMENTATION.md's known limitations), and
the checkpoint has already advanced past its leadDate - so neither the
normal poll cycle nor the retry queue will ever pick it up again on
their own.

Safety: fetches a window of real leads via get-new-leads, finds the
ONE matching --lead-id, and ignores every other lead in that window
(no risk of touching/resending anyone else). Runs a DRY PREVIEW by
default - prints the matched lead's current data and how it would
classify, but does not call WATI or update the CRM unless --confirm
is passed. This uses the exact same process_lead/process_generic_lead
functions campaign_worker calls - not a reimplementation - so a
--confirm run has the identical real-world effect a normal automatic
cycle would have had.

--test-phone lets you validate the FULL pipeline (property resolution,
template send, WATI response) against your OWN number instead of the
real lead's - without risking their real CRM record. It's not enough
to just swap the phone: process_lead() always ends by marking
lead.id's CRM record template_sent, so a test run with the real id
would falsely mark the real lead as contacted even though your own
phone received the message instead. So --test-phone also substitutes
a fake lead id (TEST-<real id>) - the CRM write then targets a record
that doesn't exist, which will show up as a failed/retrying outcome
below. That failure is EXPECTED and is exactly the point: it means the
real lead's record was never touched.

KNOWN LIMITATION, accepted rather than engineered around (3 Aug 2026):
this script runs on whatever machine you run it from, not on the
deployed service. campaign_context.remember() (called inside
process_lead) updates THIS process's own memory only - the real WATI
flow always calls the DEPLOYED service's /property-detail, a different
process that never heard about this phone number. So a lead reprocessed
this way may see blank {{project_name}} etc. fields when they tap a
button afterward, even though the template itself sends and looks
correct. Considered building a way to seed the deployed service's
memory remotely for this, but it's a rare, one-off situation and not
worth the added complexity/attack surface of a new authenticated
endpoint for it - accepting the degraded (but not broken - the
conversation still continues) experience instead when this script is
used. The NORMAL automatic pipeline (campaign_worker's own cycle) never
has this problem, since it runs entirely inside the deployed process.

Usage:
    # preview only - shows the lead and what would happen, sends nothing
    python scripts/reprocess_lead.py --lead-id housing_6eDVAQx0IacV_8850184688_1785751080 --after 2026-08-03T09:00:00.000Z

    # send for real, to the real lead's real number
    python scripts/reprocess_lead.py --lead-id housing_6eDVAQx0IacV_8850184688_1785751080 --after 2026-08-03T09:00:00.000Z --confirm

    # send for real, but to YOUR number instead - validates the pipeline
    # without touching the real lead's CRM record or actually messaging them
    python scripts/reprocess_lead.py --lead-id housing_6eDVAQx0IacV_8850184688_1785751080 --after 2026-08-03T09:00:00.000Z --confirm --test-phone 917208713112
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.indihomes_client import get_indihomes_client  # noqa: E402
from integrations.wati_client import get_wati_client  # noqa: E402
from services import campaign_service, lead_service  # noqa: E402
from utils.constants import LeadSourceCategory  # noqa: E402
from utils.helpers import normalize_phone  # noqa: E402


async def main(lead_id: str, after_date: str, confirm: bool, test_phone: str | None):
    indihomes = get_indihomes_client()
    wati = get_wati_client()

    print(f"Fetching leads since {after_date} to find {lead_id!r} ...")
    raw_leads = await indihomes.get_new_leads(after_date)
    matches = [r for r in raw_leads if r.get("id") == lead_id or r.get("_id") == lead_id]

    if not matches:
        print(f"!! No lead with id={lead_id!r} found in this window. "
              f"Try a wider --after date, or double check the id (raw dumps use 'id', not '_id').")
        return

    leads = lead_service.parse_leads(matches)
    if not leads:
        print("!! Found the raw record but it failed to parse as a Lead - check required fields (phone).")
        return

    lead = leads[0]
    category = lead_service.classify_lead(lead)

    print(f"\nMatched lead:")
    print(f"  id={lead.id}  name={lead.name!r}  phone={lead.phone}")
    print(f"  lead_source={lead.lead_source!r}  project_code={lead.project_code!r}  project_name={lead.project_name!r}")
    print(f"  current CRM status={lead.status!r}")
    print(f"  classification: {category}")

    if lead.status == "template_sent":
        print("\n!! This lead's CRM status is already 'template_sent' - it may have already gone out "
              "through some path. Double-check before proceeding, even with --confirm.")

    if category == LeadSourceCategory.IGNORED:
        print("\nThis lead is classified IGNORED - nothing would be sent even in production. Stopping.")
        return

    if test_phone:
        fake_id = f"TEST-{lead.id}"
        lead = lead.model_copy(update={"phone": normalize_phone(test_phone), "id": fake_id})
        print(f"\n--test-phone given: sending to {lead.phone} instead of the real lead's number, "
              f"using fake id={fake_id!r} so the CRM write can't touch the real record.")
        print("The CRM-update step below is EXPECTED to fail (the fake id doesn't exist on the backend) - "
              "that failure is the point, not a problem.")

    if category == LeadSourceCategory.PROPERTY_CAMPAIGN:
        print("\nNote: property details may show as blank ({{project_name}} etc.) if this lead taps a "
              "button afterward - this script runs locally, so it can't update the deployed service's own "
              "memory. The template send and CRM update below are still real. See this script's docstring "
              "for why, and why it's not worth fixing for this rare a case.")

    if not confirm:
        print(f"\nDRY PREVIEW ONLY - no message sent, no CRM update made. "
              f"Re-run with --confirm to actually process this lead for real.")
        return

    print(f"\n--confirm passed - processing for real now ...")
    if category == LeadSourceCategory.PROPERTY_CAMPAIGN:
        record = await campaign_service.process_lead(lead, indihomes, wati)
    else:
        record = await campaign_service.process_generic_lead(lead, indihomes, wati)

    print(f"\nOutcome: status={record.status}  last_error={record.last_error!r}")
    if test_phone and record.status != "template_sent":
        print("(That RETRYING/failed outcome is expected here - it's the fake-id CRM update failing, "
              "not the template send. Check your test phone's WhatsApp directly to see if the message "
              "actually arrived - that's the real signal for whether property resolution + WATI send worked.)")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--lead-id", required=True)
    parser.add_argument("--after", dest="after_date", required=True, help="afterDate wide enough to include this lead")
    parser.add_argument("--confirm", action="store_true", help="Actually send/update, not just preview")
    parser.add_argument("--test-phone", default=None, help="Send to this number instead of the lead's real one, safely (see docstring)")
    args = parser.parse_args()
    asyncio.run(main(args.lead_id, args.after_date, args.confirm, args.test_phone))
