"""Manual smoke test for the two campaign-facing HTTP endpoints:
POST /property-detail  (now returns project_name)
POST /notify-advisor    (new - replaces save-lead on the site-visit /
                          advisor branches)

Hits a running phase2-backend over real HTTP - local (uvicorn --reload)
by default, or the Railway deployment via --base-url. Does NOT touch
the real IndiHomes backend beyond whatever /property-detail itself
calls; /notify-advisor is safe to run against production repeatedly -
if BREVO_API_KEY isn't configured it just logs the email server-side
instead of sending, so nothing breaks.

Usage:
    # against local dev server
    uvicorn app:app --reload --port 8000        # in one terminal
    python scripts/smoke_test_endpoints.py       # in another

    # against Railway
    python scripts/smoke_test_endpoints.py --base-url https://indiihomes-chatbot-phase2-production.up.railway.app

    # with a real project_code from your backend, instead of the default test one
    python scripts/smoke_test_endpoints.py --project-code ETH-ORO-01 --phone 919876543210
"""
import argparse
import json
import sys

import httpx


def _print_result(label: str, resp: httpx.Response) -> None:
    print(f"\n--- {label} ---")
    print(f"status: {resp.status_code}")
    try:
        print(json.dumps(resp.json(), indent=2))
    except ValueError:
        print(resp.text)


def main(base_url: str, phone: str, project_code: str) -> None:
    base_url = base_url.rstrip("/")
    ok = True

    with httpx.Client(timeout=15) as client:
        # 1. /property-detail - confirms project_name now comes back
        resp = client.post(
            f"{base_url}/property-detail",
            json={"phone": phone, "projectCode": project_code},
        )
        _print_result("POST /property-detail", resp)
        if resp.status_code == 200:
            data = resp.json()
            if not data.get("project_name"):
                print("!! project_name is missing/empty - check the project_code exists in the backend, "
                      "or that the campaign_property_service change deployed correctly.")
                ok = False
            else:
                print(f"OK - project_name = {data['project_name']!r}")
        else:
            ok = False

        # 2. /notify-advisor - one call per reason the WATI flow will send
        cases = [
            {"phone": phone, "name": "Smoke Test", "project_code": project_code,
             "project_name": "Smoke Test Project", "reason": "advisor_requested"},
            {"phone": phone, "name": "Smoke Test", "project_code": project_code,
             "project_name": "Smoke Test Project", "reason": "site_visit_no_slots"},
            {"phone": phone, "name": "Smoke Test", "project_code": project_code,
             "project_name": "Smoke Test Project", "reason": "site_visit_booked",
             "slot_label": "Sat 2 Aug, 4:00 PM", "advisor": "Arpit"},
        ]
        for case in cases:
            resp = client.post(f"{base_url}/notify-advisor", json=case)
            _print_result(f"POST /notify-advisor ({case['reason']})", resp)
            if resp.status_code != 200:
                ok = False

    print("\n" + ("All checks passed." if ok else "Some checks failed - see above."))
    print("If BREVO_API_KEY isn't set yet, /notify-advisor still returns 200 with sent=\"no\" "
          "and logs the email server-side instead of sending it - check the phase2-backend "
          "logs to see the content that would have gone out.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--phone", default="919999999999")
    parser.add_argument("--project-code", default="ETH-ORO-01", help="Must exist in the real backend for /property-detail to resolve it")
    args = parser.parse_args()
    main(args.base_url, args.phone, args.project_code)
