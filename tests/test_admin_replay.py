"""Tests for POST /admin/replay (routes/admin.py).

This is the only endpoint in the service that can message real people
in bulk, so the tests here are mostly about what it REFUSES to do.
"""
import asyncio
import unittest
from unittest.mock import AsyncMock

from fastapi import HTTPException

from config import get_settings
from routes import admin
from utils.constants import CampaignStatus


LEADS = [
    {"_id": "keep_me", "name": "Montu Thakur", "phone": "918976394204",
     "leadSource": "Housing.com", "projectName": "Ruparel Stardom",
     "leadDate": "2026-08-31T10:32:00.000Z"},
    {"_id": "leave_me_alone", "name": "Someone Else", "phone": "919999999999",
     "leadSource": "Housing.com", "projectName": "Chaitanya Ethics Orovia",
     "leadDate": "2026-08-31T11:00:00.000Z"},
]


def _patch_clients(monkey_sent: list):
    indihomes = AsyncMock()
    indihomes.get_new_leads = AsyncMock(return_value=LEADS)
    # Dry runs call the real resolve_property; give it a client whose
    # lookups all cleanly miss, so a preview reports "unresolvable"
    # instead of tripping over AsyncMock's auto-generated return values.
    indihomes.fetch_project = AsyncMock(return_value=None)
    indihomes.fetch_project_by_name = AsyncMock(return_value=None)
    indihomes.fetch_filtered_projects = AsyncMock(return_value=[])
    wati = AsyncMock()

    admin.get_indihomes_client = lambda: indihomes
    admin.get_wati_client = lambda: wati

    async def fake_process(lead, ic, wc):
        monkey_sent.append(lead.id)
        from models.campaign import CampaignRecord
        rec = CampaignRecord(lead_id=lead.id, phone=lead.phone)
        rec.mark_sent()
        return rec

    admin.campaign_service.process_lead = fake_process
    return indihomes, wati


class AdminReplayAuthTests(unittest.TestCase):
    def setUp(self):
        get_settings.cache_clear()

    def tearDown(self):
        get_settings.cache_clear()

    def test_disabled_with_503_when_no_secret_configured(self):
        """An unconfigured deployment must be CLOSED, not open."""
        get_settings().admin_secret = ""
        with self.assertRaises(HTTPException) as ctx:
            admin._authorize("anything")
        self.assertEqual(ctx.exception.status_code, 503)

    def test_rejects_missing_or_wrong_secret(self):
        get_settings().admin_secret = "right-secret"
        for provided in (None, "", "wrong-secret"):
            with self.subTest(provided=provided):
                with self.assertRaises(HTTPException) as ctx:
                    admin._authorize(provided)
                self.assertEqual(ctx.exception.status_code, 401)

    def test_accepts_correct_secret(self):
        get_settings().admin_secret = "right-secret"
        admin._authorize("right-secret")  # must not raise


class AdminReplayBehaviourTests(unittest.TestCase):
    def setUp(self):
        get_settings.cache_clear()
        get_settings().admin_secret = "s3cret"
        self._orig_process = admin.campaign_service.process_lead

    def tearDown(self):
        admin.campaign_service.process_lead = self._orig_process
        get_settings.cache_clear()

    def _run(self, **kw):
        sent = []
        _patch_clients(sent)
        req = admin.ReplayRequest(after="2026-08-28T09:12:00.000Z", **kw)
        body = asyncio.run(admin.replay(req, x_admin_secret="s3cret"))
        return body, sent

    def test_dry_run_is_the_default_and_sends_nothing(self):
        body, sent = self._run(lead_ids=["keep_me"])
        self.assertEqual(body["mode"], "dry_run")
        self.assertEqual(sent, [])
        self.assertEqual(body["results"][0]["outcome"], "DRY RUN - nothing sent")

    def test_dry_run_survives_a_lookup_that_blows_up(self):
        """A preview exists to show all 17 leads at once; one bad
        lookup must not 500 the batch and hide the other 16."""
        async def boom(*_a, **_kw):
            raise RuntimeError("backend down")
        orig = admin.property_service.resolve_property
        admin.property_service.resolve_property = boom
        try:
            body, sent = self._run(lead_ids=["keep_me", "leave_me_alone"])
        finally:
            admin.property_service.resolve_property = orig
        self.assertEqual(sent, [])
        self.assertEqual(len(body["results"]), 2)
        self.assertTrue(all(r["would_resolve_to"] is None for r in body["results"]))

    def test_confirm_sends_only_the_listed_lead(self):
        """The allow-list is the core safety property: a lead in the
        fetched window but NOT named must never be messaged."""
        body, sent = self._run(lead_ids=["keep_me"], confirm=True)
        self.assertEqual(body["mode"], "SENT")
        self.assertEqual(sent, ["keep_me"])
        self.assertNotIn("leave_me_alone", sent)
        self.assertEqual(body["results"][0]["outcome"], CampaignStatus.TEMPLATE_SENT)

    def test_reports_ids_not_found_in_the_window(self):
        body, _ = self._run(lead_ids=["keep_me", "ghost_id"], confirm=True)
        self.assertEqual(body["not_found_in_window"], ["ghost_id"])
        self.assertEqual(body["found_in_window"], 1)

    def test_replayed_lead_is_marked_processed_so_the_poller_skips_it(self):
        from workers import campaign_worker
        campaign_worker._processed_lead_ids.discard("keep_me")
        self._run(lead_ids=["keep_me"], confirm=True)
        self.assertIn("keep_me", campaign_worker._processed_lead_ids)


if __name__ == "__main__":
    unittest.main()
