"""Regression tests for the 3 Sep 2026 project-name resolution fix.

Two changes are covered:

1. property_service's tier-4 partial (contained) name match, which
   resolves the real Housing.com-vs-catalogue naming mismatches that
   silently failed for 14 of 67 real leads between 25 Aug and 3 Sep.
2. campaign_service treating "no catalogue match" as PERMANENT
   (advisor-notified) rather than something to retry 3 times over ~80
   minutes.

Every project name below is copied verbatim from the live Indihomes
catalogue (153 projects, fetched 3 Sep 2026), trailing spaces and all -
the whitespace inconsistencies are real stored data, not typos here.
"""
import asyncio
import unittest
from unittest.mock import AsyncMock

from models.lead import Lead
from services import campaign_service, property_service
from utils.constants import CampaignStatus

# A representative slice of the real catalogue: the four names that
# already resolved exactly, the three that only a partial match can
# reach, and enough near-misses to prove the guards work.
CATALOGUE = [
    {"id": "oriwV8uBVkI6", "projectName": "INV_MW_441", "displayName": "Chaitanya Ethics Orovia"},
    {"id": "a1", "projectName": "INV_1", "displayName": "38 Avenue By Artha Lifespaces "},
    {"id": "a2", "projectName": "INV_2", "displayName": "Chandak Treesourus"},
    {"id": "a3", "projectName": "INV_3", "displayName": "Mahindra Marina"},
    {"id": "a4", "projectName": "INV_4", "displayName": "Mahindra Vista"},
    {"id": "a5", "projectName": "INV_5", "displayName": "Ruparel Stardom"},
    {"id": "a6", "projectName": "INV_6", "displayName": "Asmi Legacy"},
    {"id": "a7", "projectName": "INV_7", "displayName": "AnchorPoint Aviara"},
    {"id": "a8", "projectName": "INV_8", "displayName": "Astral"},
    {"id": "a9", "projectName": "INV_9", "displayName": " Sahakar Ved "},
    {"id": "a10", "projectName": "INV_10", "displayName": "Sahakar Vogue 77"},
    {"id": "a11", "projectName": "INV_11", "displayName": "Chaitanya Ganapati Baug"},
]


def _client(catalogue=None):
    """A client whose exact-name and searchText lookups both miss, so
    every test below exercises tier 4 specifically."""
    client = AsyncMock()
    client.fetch_project = AsyncMock(return_value=None)
    client.fetch_project_by_name = AsyncMock(return_value=None)

    async def _filtered(filters):
        # Tier 3 passes searchText; tier 4 passes limit only.
        if "searchText" in filters:
            return []
        return CATALOGUE if catalogue is None else catalogue

    client.fetch_filtered_projects = AsyncMock(side_effect=_filtered)
    return client


class PartialNameMatchTests(unittest.TestCase):
    def setUp(self):
        property_service._all_projects_cache = None

    def _resolve(self, name, catalogue=None):
        return asyncio.run(
            property_service.resolve_raw_project(_client(catalogue), None, name)
        )

    # --- the three real mismatches this fix exists for ---------------

    def test_lead_name_shorter_than_catalogue_name_builder_prefix(self):
        """Housing.com: "Treesourus". Catalogue: "Chandak Treesourus"."""
        self.assertEqual(self._resolve("Treesourus")["displayName"], "Chandak Treesourus")

    def test_lead_name_shorter_than_catalogue_name_builder_suffix(self):
        """Housing.com: "38 Avenue". Catalogue: "38 Avenue By Artha Lifespaces "."""
        self.assertEqual(
            self._resolve("38 Avenue")["displayName"], "38 Avenue By Artha Lifespaces "
        )

    def test_lead_name_longer_than_catalogue_name_tower_suffix(self):
        """Housing.com: "Mahindra Marina 64 Phase 3". Catalogue: "Mahindra Marina".

        Also proves the uniqueness rule does real work: "Mahindra Vista"
        shares the first token and must NOT be considered a match.
        """
        self.assertEqual(
            self._resolve("Mahindra Marina 64 Phase 3")["displayName"], "Mahindra Marina"
        )

    # --- the guards --------------------------------------------------

    def test_short_generic_name_never_claims_a_longer_name(self):
        """"Astral" (6 chars) is under _MIN_CONTAINMENT_CHARS, so a lead
        for "Astral Heights Phase 2" must not be attached to it."""
        self.assertIsNone(self._resolve("Astral Heights Phase 2"))

    def test_tokens_must_be_whole_and_contiguous(self):
        """Substring matching would attach "One Vara" to "Stone
        Varanasi"; whole-token matching must not."""
        catalogue = [{"id": "x", "displayName": "Stone Varanasi Residency"}]
        self.assertIsNone(self._resolve("One Vara", catalogue))

    def test_ambiguous_match_resolves_nothing_rather_than_guessing(self):
        catalogue = [
            {"id": "x", "displayName": "Green Acres Phase 1"},
            {"id": "y", "displayName": "Green Acres Phase 2"},
        ]
        self.assertIsNone(self._resolve("Green Acres", catalogue))

    def test_genuinely_unstocked_project_still_resolves_nothing(self):
        """The four names that really aren't in the catalogue must keep
        failing - this fix must not invent a match for them."""
        for name in ("Narang Vivenda", "L And T Ahana Tower A",
                     "Sahakar Revanta", "Runwal Auris Serenity Tower 4 Residential"):
            with self.subTest(name=name):
                self.assertIsNone(self._resolve(name))

    def test_exact_match_still_wins_and_never_reaches_tier_4(self):
        client = _client()
        client.fetch_project_by_name = AsyncMock(
            return_value={"id": "a5", "displayName": "Ruparel Stardom"}
        )
        raw = asyncio.run(property_service.resolve_raw_project(client, None, "Ruparel Stardom"))
        self.assertEqual(raw["displayName"], "Ruparel Stardom")
        client.fetch_filtered_projects.assert_not_awaited()

    def test_catalogue_is_cached_across_calls(self):
        client = _client()
        asyncio.run(property_service.resolve_raw_project(client, None, "Treesourus"))
        asyncio.run(property_service.resolve_raw_project(client, None, "38 Avenue"))
        catalogue_calls = [
            c for c in client.fetch_filtered_projects.await_args_list
            if "searchText" not in c.args[0]
        ]
        self.assertEqual(len(catalogue_calls), 1)

    def test_empty_catalogue_is_not_cached(self):
        """An empty result is far more likely to be a backend hiccup than
        a genuinely empty catalogue - caching it would blind resolution
        for the full TTL."""
        client = _client(catalogue=[])
        asyncio.run(property_service.resolve_raw_project(client, None, "Treesourus"))
        self.assertIsNone(property_service._all_projects_cache)


class UnresolvedProjectIsPermanentTests(unittest.TestCase):
    def setUp(self):
        property_service._all_projects_cache = None

    def test_no_catalogue_match_notifies_advisor_instead_of_retrying(self):
        lead = Lead.model_validate({
            "_id": "housing_test_1", "name": "Test Lead", "phone": "919999999999",
            "leadSource": "Housing.com", "projectName": "Narang Vivenda",
            "leadDate": "2026-09-03T04:29:00.000Z",
        })
        notified = []
        original = campaign_service.notify_service.notify_advisor
        campaign_service.notify_service.notify_advisor = lambda req: notified.append(req)
        try:
            record = asyncio.run(
                campaign_service.process_lead(lead, _client(), AsyncMock())
            )
        finally:
            campaign_service.notify_service.notify_advisor = original

        self.assertEqual(record.status, CampaignStatus.ADVISOR_NOTIFIED)
        self.assertNotEqual(record.status, CampaignStatus.RETRYING)
        self.assertEqual(record.attempts, 0, "must not consume a retry attempt")
        self.assertEqual(len(notified), 1)
        self.assertEqual(notified[0].reason, "unresolved_project")
        self.assertEqual(notified[0].project_name, "Narang Vivenda")


if __name__ == "__main__":
    unittest.main()
