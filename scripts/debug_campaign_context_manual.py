"""Manual debug script - NOT a pytest test (moved out of tests/ on
4 Aug 2026: it used to live there as test_campaign_context.py, which
meant pytest executed asyncio.run(main()) - including a REAL network
call to the production IndiHomes backend and a REAL write to
state/campaign_context.json - just from importing the file during test
collection, every single time `pytest` ran, regardless of which tests
were actually being run. That's the confirmed source of the
919876543210 -> INV_GW_547 entry found in the real campaign_context.json
during the 4 Aug 2026 review.

Mostly superseded by scripts/diagnose_issues.py --lookup-project /
--phone, which covers the same ground more thoroughly. Kept here for
reference; safe to delete if diagnose_issues.py covers your needs.
"""
import asyncio

from integrations.indihomes_client import get_indihomes_client
from services import campaign_context, property_service
from models.lead import Lead


async def main():
    client = get_indihomes_client()

    lead = Lead(
        id="debug-lead",
        phone="919876543210",
        name="Debug User",
        project_name="Kolte Patil Verve",
        project_code=None,
        lead_source="Debug",
    )

    prop = await property_service.resolve_property(client, lead)

    print("\nResolved Property")
    print("-----------------")
    print("Project Name :", prop.project_name)
    print("Project Code :", prop.project_code)

    campaign_context.remember(
        lead.phone,
        prop.project_code,
    )

    print("\nStored in campaign_context")
    print("--------------------------")
    print(
        campaign_context.get_project_code(
            lead.phone
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
