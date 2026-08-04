"""Diagnostic script for two production bugs:

  1. DUPLICATE MESSAGES  - tests whether the sent_template_store (disk ledger)
     and _processed_lead_ids are working, and checks what state/sent_templates.json
     currently says for specific leads.

  2. {{project_name}} NOT RESOLVING - tests the /property-detail endpoint directly
     to see if it can resolve a project for a given phone, and reveals whether the
     campaign_context in-memory store is the bottleneck.

Usage:
    # Check the disk ledger for a specific lead
    python scripts/diagnose_issues.py --lead-id <ID>

    # Test /property-detail for a phone (deployed service)
    python scripts/diagnose_issues.py --phone 919876543210

    # Test /property-detail with an explicit project_code
    python scripts/diagnose_issues.py --phone 919876543210 --project-code "Kolte Patil Verve"

    # Full combo: check lead ledger AND test property resolution
    python scripts/diagnose_issues.py --lead-id <ID> --phone 919876543210

    # Test project lookup by name against the backend directly
    python scripts/diagnose_issues.py --lookup-project "Kolte Patil Verve"

    # Show the full sent_templates ledger (all entries)
    python scripts/diagnose_issues.py --dump-ledger
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx  # noqa: E402

from config import get_settings  # noqa: E402
from integrations.indihomes_client import get_indihomes_client  # noqa: E402
from services import campaign_context, property_service  # noqa: E402
from utils import sent_template_store  # noqa: E402
from utils.helpers import normalize_phone  # noqa: E402

SECTION = "=" * 60


def _check_ledger(lead_id: str) -> None:
    """Check the disk-persisted sent_templates.json for a specific lead."""
    print(f"\n{SECTION}")
    print(f"[1] DISK LEDGER CHECK for lead_id={lead_id!r}")
    print(SECTION)

    ledger_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "state", "sent_templates.json"
    )
    print(f"Ledger path: {ledger_path}")
    if not os.path.exists(ledger_path):
        print("!! state/sent_templates.json does NOT EXIST.")
        print("   NOTE: this is expected/normal if no lead has been sent a template")
        print("   since this container started - has_sent()/mark_sent() only ever")
        print("   write to this file on an actual send, they don't create it upfront.")
        print("   Only a real concern if it's STILL missing after a real send has")
        print("   happened since this deploy went live.")
        return

    with open(ledger_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    sent = data.get("sent", {})
    print(f"   Total entries in ledger: {len(sent)}")

    # Find entries matching this lead_id
    matches = {k: v for k, v in sent.items() if k.startswith(f"{lead_id}:")}
    if matches:
        print(f"   FOUND {len(matches)} entry/entries for lead_id={lead_id!r}:")
        for k, v in matches.items():
            print(f"     {k}: {v}")
        print("   -> This lead IS in the ledger. A restart won't resend it.")
    else:
        print(f"   NO entries found for lead_id={lead_id!r}.")
        print("   -> This lead is NOT in the ledger. After a restart, it could be resent")
        print("      IF it's still within the checkpoint window AND the CRM status wasn't updated.")


def _dump_ledger() -> None:
    """Dump the full sent_templates.json content."""
    print(f"\n{SECTION}")
    print("[LEDGER DUMP] Full state/sent_templates.json contents")
    print(SECTION)

    ledger_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "state", "sent_templates.json"
    )
    if not os.path.exists(ledger_path):
        print("!! state/sent_templates.json does NOT EXIST yet.")
        print("   Expected/normal if nothing's been sent since this container started -")
        print("   see the note in _check_ledger for the same point in more detail.")
        return

    with open(ledger_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    print(json.dumps(data, indent=2))
    sent = data.get("sent", {})
    print(f"\nTotal: {len(sent)} entry/entries.")


async def _test_property_detail_endpoint(base_url: str, phone: str, project_code: str | None) -> None:
    """Hit the deployed /property-detail endpoint directly to test Issue 2."""
    print(f"\n{SECTION}")
    print(f"[2] /property-detail ENDPOINT TEST")
    print(f"    phone={phone!r}  project_code={project_code!r}")
    print(SECTION)

    payload: dict = {"phone": phone}
    if project_code:
        payload["projectCode"] = project_code

    # Use the deployed service URL. For local testing, use http://localhost:8000.
    # We try localhost first, then ask the user to specify.
    for candidate_base in ["http://localhost:8000", "http://127.0.0.1:8000"]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(f"{candidate_base}/property-detail", json=payload)
            print(f"  -> Hit {candidate_base}/property-detail: status={resp.status_code}")
            print(f"  -> Response: {resp.text[:2000]}")
            data = resp.json()
            found = data.get("found", "?")
            project_name = data.get("project_name", "")
            if found == "yes" and project_name:
                print(f"\n  ✅ Project resolved OK: project_name={project_name!r}")
            elif found == "yes" and not project_name:
                print(f"\n  ⚠️  found=yes but project_name is EMPTY string - check raw_to_property()")
            else:
                print(f"\n  ❌ found={found!r} - property-detail could NOT resolve the project.")
                print("     This is why WATI shows {{project_name}} etc. - the flow got found=no")
                print("     and the template variables were never populated.")
            return
        except (httpx.ConnectError, httpx.ConnectTimeout):
            pass  # Try next

    print("  !! Could not connect to localhost:8000. Is the service running locally?")
    print("  If you want to test against the deployed Railway service, run:")
    print("    curl -X POST https://<your-railway-url>/property-detail \\")
    print(f"         -H 'Content-Type: application/json' \\")
    print(f"         -d '{json.dumps(payload)}'")


async def _lookup_project_by_name(name: str) -> None:
    """Test all three tiers of resolve_raw_project for a given project name."""
    print(f"\n{SECTION}")
    print(f"[3] PROJECT LOOKUP TEST for name={name!r}")
    print(SECTION)

    client = get_indihomes_client()

    # Tier 1: fetch_project (treats name as an id)
    print(f"  Tier 1: fetch_project(id={name!r}) ...")
    raw = await client.fetch_project(name)
    if raw:
        print(f"    ✅ Found via fetch_project(id). Keys: {list(raw.keys())}")
        print(f"    displayName={raw.get('displayName')!r}  projectCode={raw.get('projectCode')!r}")
    else:
        print(f"    ❌ Not found via fetch_project(id={name!r})")

    # Tier 2: fetch_project_by_name
    print(f"\n  Tier 2: fetch_project_by_name(projectName={name!r}) ...")
    raw2 = await client.fetch_project_by_name(name)
    if raw2:
        print(f"    ✅ Found via fetch_project_by_name. Keys: {list(raw2.keys())}")
        print(f"    displayName={raw2.get('displayName')!r}  projectCode={raw2.get('projectCode')!r}")
    else:
        print(f"    ❌ Not found via fetch_project_by_name(projectName={name!r})")

    # Tier 3: searchText with first word
    first_word = name.split()[0] if name.split() else name
    print(f"\n  Tier 3: fetch_filtered_projects(searchText={first_word!r}, limit=20) ...")
    candidates = await client.fetch_filtered_projects({"searchText": first_word, "limit": 20})
    print(f"    Returned {len(candidates)} candidates.")
    for c in candidates:
        display = c.get("displayName") or c.get("projectName") or ""
        code = c.get("projectCode") or c.get("id") or ""
        print(f"    - displayName={display!r}  projectCode={code!r}")

    # Full resolve_raw_project call
    print(f"\n  Full resolve_raw_project(code={name!r}, name={name!r}) ...")
    raw_final = await property_service.resolve_raw_project(client, name, name)
    if raw_final:
        prop = property_service.raw_to_property(raw_final, fallback_code=name, fallback_name=name)
        print(f"    ✅ Resolved! project_name={prop.project_name!r}  project_code={prop.project_code!r}")
        print(f"    location={prop.location!r}  price_range={prop.price_range!r}")
        print(f"    configurations={prop.configurations!r}  carpet_area={prop.carpet_area!r}")
        print(f"    possession_date={prop.possession_date!r}")
    else:
        print(f"    ❌ resolve_raw_project returned None - ALL THREE TIERS FAILED.")
        print(f"    This IS the bug: the project cannot be found by code, name, or search.")
        print(f"    Options:")
        print(f"      a) The project name in the CRM lead doesn't match any displayName/projectName")
        print(f"         in the backend. Run: python scripts/list_projects.py --search {first_word!r}")
        print(f"         to see what names the backend actually has.")
        print(f"      b) The project exists but the WATI flow is passing a WATI placeholder")
        print(f"         like '{{{{project_name}}}}' because the contact attribute was never set.")


async def _check_campaign_context_store() -> None:
    """Check the campaign context store (in-memory + disk if persisted).

    IMPORTANT (fixed 4 Aug 2026): this used to check ONLY whether
    state/campaign_context.json exists on disk, and printed "in-memory
    ONLY, here's the fix" whenever it didn't - conflating two very
    different situations: the persistence CODE genuinely missing from
    this deploy, vs. the code being present but no lead having
    triggered a write yet on a fresh container. That caused a real,
    confirmed false alarm: a freshly-redeployed service (right after
    fixing an unrelated volume-mount crash loop) hadn't processed any
    real Property Campaign lead yet, so campaign_context.json didn't
    exist - and this check reported the OLD, already-fixed bug as if
    it were still present, sending troubleshooting in the wrong
    direction. Now checks the CODE first (does campaign_context have
    _CONTEXT_PATH at all?), completely independent of whether the file
    happens to exist yet.
    """
    print(f"\n{SECTION}")
    print("[4] CAMPAIGN CONTEXT STORE CHECK")
    print(SECTION)

    has_persistence_code = hasattr(campaign_context, "_CONTEXT_PATH")
    if not has_persistence_code:
        print("  ❌ services/campaign_context.py has NO _CONTEXT_PATH attribute.")
        print("     This deployment is running the OLD in-memory-only version - the")
        print("     persistence fix genuinely was not included in whatever was deployed.")
        print("     Fix: make sure services/campaign_context.py with the disk-persistence")
        print("     code is actually committed, pushed, and part of the running deploy.")
        return

    print("  ✅ campaign_context.py HAS disk-persistence code (_CONTEXT_PATH present) -")
    print("     the fix IS in this deployment's code, regardless of what's below.")

    context_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "state", "campaign_context.json"
    )
    if os.path.exists(context_path):
        print(f"\n  ✅ Persisted context store found at {context_path}")
        with open(context_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        entries = data.get("context", {})
        print(f"  Entries: {len(entries)}")
        for phone, code in list(entries.items())[:10]:
            print(f"    phone={phone}  -> project_code={code!r}")
        if len(entries) > 10:
            print(f"    ... and {len(entries) - 10} more")
    else:
        print(f"\n  ℹ️  No file yet at {context_path}.")
        print("     NORMAL for a fresh deploy/volume - remember() only writes on an actual")
        print("     Property Campaign send, it doesn't create the file upfront. Only a real")
        print("     concern if this is STILL empty after a real Property Campaign lead has")
        print("     been sent a template since this deploy started - if so, check whether a")
        print("     Railway Volume is actually mounted at the right path (see DOCUMENTATION.md).")


async def main(
    lead_id: str | None,
    phone: str | None,
    project_code: str | None,
    lookup_project: str | None,
    dump_ledger: bool,
) -> None:
    settings = get_settings()
    print(f"INDIHOMES_BASE_URL={settings.indihomes_base_url}")
    print(f"WATI_TEMPLATE_NAME={settings.wati_template_name}")
    print(f"WATI_GENERIC_TEMPLATE_NAME={settings.wati_generic_template_name}")

    if dump_ledger:
        _dump_ledger()

    if lead_id:
        _check_ledger(lead_id)

    if phone:
        norm = normalize_phone(phone)
        await _test_property_detail_endpoint(settings.indihomes_base_url, norm or phone, project_code)

    if lookup_project:
        await _lookup_project_by_name(lookup_project)

    await _check_campaign_context_store()

    if not any([lead_id, phone, lookup_project, dump_ledger]):
        print("\nNo specific checks requested. Run with --help to see options.")
        print("Running context store check only (above).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnose duplicate-send and {{project_name}} issues")
    parser.add_argument("--lead-id", default=None, help="Check the disk ledger for this lead ID")
    parser.add_argument("--phone", default=None, help="Test /property-detail for this phone number")
    parser.add_argument("--project-code", default=None, help="Pass projectCode to /property-detail (simulates WATI flow)")
    parser.add_argument("--lookup-project", default=None, help="Test all 3 lookup tiers for this project name/code")
    parser.add_argument("--dump-ledger", action="store_true", help="Print the full sent_templates.json ledger")
    args = parser.parse_args()
    asyncio.run(main(args.lead_id, args.phone, args.project_code, args.lookup_project, args.dump_ledger))
