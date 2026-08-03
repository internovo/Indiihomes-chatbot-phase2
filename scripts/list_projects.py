"""Safe, read-only lookup of real projects from the IndiHomes backend.
Two modes:

  --limit N         list the first N projects (original behavior)
  --search TEXT      search using the same "searchText" parameter the
                      public website's own search box uses (visible in
                      its URL, e.g. indihomes.co.in/properties?searchText=Ariha)
                      - useful when fetchProjectByName's exact-match
                      lookup 404s for a project you can visibly confirm
                      is real on the website, since this may tolerate
                      spelling/spacing differences that an exact match
                      won't.

Prints the FULL raw project dict (not just a guessed "code" field) -
fetch_project's docstring warns the backend wants the project's `id`
field specifically, not `projectCode`/`projectName`, so this shows
every id-shaped field rather than assuming which one is right.

Calls the same fetch_filtered_projects wrapper the rest of the service
uses - no writes, no side effects.

Usage:
    python scripts/list_projects.py
    python scripts/list_projects.py --limit 20
    python scripts/list_projects.py --search Ariha
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from integrations.indihomes_client import get_indihomes_client  # noqa: E402


async def main(limit: int, search: str | None):
    client = get_indihomes_client()
    filters = {"limit": limit}
    if search:
        filters["searchText"] = search
        print(f"Searching for projects matching searchText={search!r} (limit={limit}) ...")
    else:
        print(f"Fetching up to {limit} project(s) ...")

    try:
        projects = await client.fetch_filtered_projects(filters)
    except Exception as exc:  # noqa: BLE001 - this is a diagnostic script
        print(f"!! request failed: {exc}")
        return

    if not projects:
        if search:
            print(f"Backend returned 0 projects matching {search!r} - either it's genuinely not in this "
                  f"backend yet (even though it's live on the public website), or 'searchText' isn't the "
                  f"right filter param name for fetchPaginatedFilteredProjectList specifically. Worth trying "
                  f"without --search to see the full list and eyeball it manually.")
        else:
            print("Backend returned 0 projects - check INDIHOMES_API_KEY / INDIHOMES_BASE_URL in .env.")
        return

    print(f"Got {len(projects)} project(s). Full raw dict for each:\n")
    for p in projects:
        print(json.dumps(p, indent=2, default=str))
        print("-" * 60)

    print("\nLook for an 'id' / '_id' field specifically - fetch_project sends whatever you give it "
          "as `id`, and per its docstring that's NOT the same as projectCode/projectName. Then test with:")
    print("  python scripts/smoke_test_endpoints.py --project-code <the id value> --phone <real phone>")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=3)
    parser.add_argument("--search", type=str, default=None, help="Search by name, e.g. --search Ariha")
    args = parser.parse_args()
    asyncio.run(main(args.limit, args.search))
