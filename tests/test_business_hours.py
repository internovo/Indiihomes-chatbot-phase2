"""Tests for business-hours gating in the Phase 2 Campaign Service:
business_hours.py (same file as Phase 1's, copied unchanged),
utils/pending_queue.py, the gate wired into
services/campaign_service.py's process_lead / process_generic_lead,
workers/campaign_worker.py's updated bookkeeping, and
workers/queue_flush_worker.py's daily drain.

Run: pytest tests/test_business_hours.py -v

IMPORTANT ON PATCHING is_business_hours: campaign_service.py does
`from business_hours import is_business_hours` - a name binding INTO
campaign_service's own module namespace, not a reference back to
business_hours.py. Patching "business_hours.is_business_hours" would
have NO EFFECT on campaign_service's calls - the patch target must be
"services.campaign_service.is_business_hours", where the name is
actually looked up from. Same lesson already documented in Phase 1's
claude.md (the reply_text/{{ }} vs @ WATI variable-syntax bugs) and in
Phase 1's own tests/test_business_hours.py - noted here explicitly
because it's an easy mistake to reintroduce in a second codebase.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import business_hours
from models.campaign import CampaignRecord
from models.lead import Lead
from services import campaign_service
from utils import pending_queue
from utils.constants import CampaignStatus
from workers import campaign_worker, queue_flush_worker, retry_worker

IST = ZoneInfo("Asia/Kolkata")


def _ist(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=IST)


# --------------------------------------------------------------------------
# business_hours.py itself - same boundary tests as Phase 1's, since this
# is meant to be the identical file. Kept here too (not just "trust Phase
# 1's tests") so a future edit to THIS copy that silently diverges from
# Phase 1's gets caught in whichever project's suite runs.
# --------------------------------------------------------------------------

class TestIsBusinessHours:
    def test_mid_morning_is_open(self):
        assert business_hours.is_business_hours(_ist(2026, 8, 6, 11, 0))

    def test_exact_open_boundary_is_open(self):
        assert business_hours.is_business_hours(_ist(2026, 8, 6, 10, 0))

    def test_exact_close_boundary_is_open(self):
        assert business_hours.is_business_hours(_ist(2026, 8, 6, 19, 0))

    def test_one_minute_before_open_is_closed(self):
        assert not business_hours.is_business_hours(_ist(2026, 8, 6, 9, 59))

    def test_one_minute_after_close_is_closed(self):
        assert not business_hours.is_business_hours(_ist(2026, 8, 6, 19, 1))


class TestNextBusinessOpen:
    def test_before_open_rolls_to_today_10am(self):
        assert business_hours.next_business_open(_ist(2026, 8, 6, 6, 30)) == _ist(2026, 8, 6, 10, 0)

    def test_after_close_rolls_to_tomorrow_10am(self):
        assert business_hours.next_business_open(_ist(2026, 8, 6, 20, 15)) == _ist(2026, 8, 7, 10, 0)


# --------------------------------------------------------------------------
# utils/pending_queue.py - persistence round trip
# --------------------------------------------------------------------------

def _make_lead(lead_id="1", phone="9876543210", project_code="P1", project_name="Some Project"):
    return Lead(_id=lead_id, phone=phone, name="Test User", leadSource="Housing.com",
                projectCode=project_code, projectName=project_name)


class TestPendingQueue:
    def test_empty_queue_by_default(self):
        assert pending_queue.load_all() == []
        assert pending_queue.pending_count() == 0
        assert pending_queue.oldest_queued_at() is None

    def test_enqueue_and_load(self):
        lead = _make_lead()
        pending_queue.enqueue(lead, category="property_campaign")

        entries = pending_queue.load_all()
        assert len(entries) == 1
        assert entries[0]["lead_id"] == "1"
        assert entries[0]["category"] == "property_campaign"
        assert pending_queue.pending_count() == 1
        assert pending_queue.oldest_queued_at() is not None

    def test_to_lead_reconstructs_equivalent_lead(self):
        lead = _make_lead()
        pending_queue.enqueue(lead, category="property_campaign")
        entry = pending_queue.load_all()[0]
        rebuilt = pending_queue.to_lead(entry)

        assert rebuilt.id == lead.id
        assert rebuilt.phone == lead.phone
        assert rebuilt.project_code == lead.project_code
        assert rebuilt.project_name == lead.project_name

    def test_reenqueue_same_lead_id_updates_in_place_not_duplicated(self):
        lead = _make_lead()
        pending_queue.enqueue(lead, category="property_campaign")
        first_queued_at = pending_queue.load_all()[0]["queued_at"]

        # Re-queue the SAME lead (simulates the flush job's defensive
        # re-check finding it's still off-hours) - must not duplicate,
        # and must preserve the ORIGINAL queued_at (oldest-first order
        # reflects when it FIRST went off-hours).
        pending_queue.enqueue(lead, category="property_campaign")

        entries = pending_queue.load_all()
        assert len(entries) == 1
        assert entries[0]["queued_at"] == first_queued_at

    def test_remove_many_only_removes_given_ids(self):
        pending_queue.enqueue(_make_lead(lead_id="1"), category="property_campaign")
        pending_queue.enqueue(_make_lead(lead_id="2"), category="generic_interest")
        pending_queue.enqueue(_make_lead(lead_id="3"), category="property_campaign")

        pending_queue.remove_many(["1", "3"])

        remaining = pending_queue.load_all()
        assert len(remaining) == 1
        assert remaining[0]["lead_id"] == "2"

    def test_oldest_first_ordering_preserved(self):
        pending_queue.enqueue(_make_lead(lead_id="1"), category="property_campaign")
        pending_queue.enqueue(_make_lead(lead_id="2"), category="property_campaign")
        pending_queue.enqueue(_make_lead(lead_id="3"), category="property_campaign")

        entries = pending_queue.load_all()
        assert [e["lead_id"] for e in entries] == ["1", "2", "3"]


# --------------------------------------------------------------------------
# services/campaign_service.py - the gate itself
# --------------------------------------------------------------------------

class FakeIndihomesClient:
    def __init__(self, project=None):
        self._project = project
        self.updated_leads = []

    async def fetch_project(self, project_id):
        return self._project

    async def fetch_project_by_name(self, project_name):
        return self._project

    async def update_lead(self, lead_id, payload):
        self.updated_leads.append((lead_id, payload))
        return {"ok": True}


class FakeWatiClient:
    def __init__(self):
        self.sent_templates = []

    async def send_template(self, phone, template_name, parameters):
        self.sent_templates.append((phone, template_name, parameters))
        return {"result": "success"}


@pytest.fixture(autouse=True)
def fake_email_client(monkeypatch):
    """process_lead's "no project data" defensive path can email an
    advisor - never let that hit the real Brevo API from these tests."""
    class FakeEmail:
        def send(self, to, cc, subject, body):
            return True
    monkeypatch.setattr("services.notify_service.get_email_client", lambda: FakeEmail())


@pytest.mark.asyncio
async def test_process_lead_queues_off_hours_instead_of_sending(monkeypatch):
    monkeypatch.setattr("services.campaign_service.is_business_hours", lambda: False)
    lead = _make_lead()
    indihomes = FakeIndihomesClient(project={"projectCode": "P1", "projectName": "Some Project"})
    wati = FakeWatiClient()

    record = await campaign_service.process_lead(lead, indihomes, wati)

    assert record.status == CampaignStatus.QUEUED_OFF_HOURS
    assert len(wati.sent_templates) == 0
    # CRM must NOT be updated - the lead hasn't actually been messaged yet.
    assert len(indihomes.updated_leads) == 0
    assert pending_queue.pending_count() == 1


@pytest.mark.asyncio
async def test_process_generic_lead_queues_off_hours_instead_of_sending(monkeypatch):
    monkeypatch.setattr("services.campaign_service.is_business_hours", lambda: False)
    lead = Lead(_id="1", phone="9876543210", name="Priya", leadSource="Meta Ads")
    indihomes = FakeIndihomesClient()
    wati = FakeWatiClient()

    record = await campaign_service.process_generic_lead(lead, indihomes, wati)

    assert record.status == CampaignStatus.QUEUED_OFF_HOURS
    assert len(wati.sent_templates) == 0
    assert len(indihomes.updated_leads) == 0
    assert pending_queue.pending_count() == 1


@pytest.mark.asyncio
async def test_process_lead_sends_normally_within_business_hours(monkeypatch):
    monkeypatch.setattr("services.campaign_service.is_business_hours", lambda: True)
    lead = _make_lead()
    indihomes = FakeIndihomesClient(project={"projectCode": "P1", "projectName": "Some Project"})
    wati = FakeWatiClient()

    record = await campaign_service.process_lead(lead, indihomes, wati)

    assert record.status == CampaignStatus.TEMPLATE_SENT
    assert len(wati.sent_templates) == 1
    assert pending_queue.pending_count() == 0


@pytest.mark.asyncio
async def test_already_sent_lead_is_not_requeued_even_off_hours(monkeypatch):
    """The idempotency-ordering guarantee: has_sent() must be checked
    BEFORE the business-hours gate, so a lead that was already sent
    (e.g. right at the 6:59 PM edge, then reprocessed somehow) is never
    queued a second time."""
    monkeypatch.setattr("services.campaign_service.is_business_hours", lambda: False)
    lead = _make_lead()
    indihomes = FakeIndihomesClient(project={"projectCode": "P1", "projectName": "Some Project"})
    wati = FakeWatiClient()

    from utils import sent_template_store
    sent_template_store.mark_sent("1", "campaign_property_intro")

    record = await campaign_service.process_lead(lead, indihomes, wati)

    assert record.status == CampaignStatus.TEMPLATE_SENT
    assert pending_queue.pending_count() == 0
    assert len(wati.sent_templates) == 0  # not RE-sent either


# --------------------------------------------------------------------------
# workers/campaign_worker.py - bookkeeping for the new status
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clear_retry_queue():
    retry_worker._queue.clear()
    yield
    retry_worker._queue.clear()


def test_summarize_and_queue_counts_queued_off_hours_and_skips_retry_queue():
    lead = _make_lead()
    record = CampaignRecord(lead_id="1", phone="9876543210")
    record.status = CampaignStatus.QUEUED_OFF_HOURS

    sent, queued, notified, queued_off_hours = campaign_worker._summarize_and_queue(
        [record], [lead], campaign_service.process_lead,
    )

    assert (sent, queued, notified, queued_off_hours) == (0, 0, 0, 1)
    # Must NOT have entered retry_worker's backoff queue - this is not a failure.
    assert retry_worker.pending_count() == 0


# --------------------------------------------------------------------------
# workers/queue_flush_worker.py - the daily drain
# --------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flush_sends_queued_leads_when_now_within_business_hours(monkeypatch):
    monkeypatch.setattr("services.campaign_service.is_business_hours", lambda: True)

    lead = _make_lead()
    pending_queue.enqueue(lead, category="property_campaign")
    assert pending_queue.pending_count() == 1

    wati = FakeWatiClient()
    indihomes = FakeIndihomesClient(project={"projectCode": "P1", "projectName": "Some Project"})
    monkeypatch.setattr("workers.queue_flush_worker.get_indihomes_client", lambda: indihomes)
    monkeypatch.setattr("workers.queue_flush_worker.get_wati_client", lambda: wati)

    await queue_flush_worker.run_cycle()

    assert len(wati.sent_templates) == 1
    assert pending_queue.pending_count() == 0


@pytest.mark.asyncio
async def test_flush_leaves_entry_queued_if_somehow_still_off_hours(monkeypatch):
    """Defensive case: if the flush job's schedule and business_hours
    ever drift out of sync, a re-queued lead must stay in the queue,
    not silently vanish."""
    monkeypatch.setattr("services.campaign_service.is_business_hours", lambda: False)

    lead = _make_lead()
    pending_queue.enqueue(lead, category="property_campaign")

    wati = FakeWatiClient()
    indihomes = FakeIndihomesClient(project={"projectCode": "P1", "projectName": "Some Project"})
    monkeypatch.setattr("workers.queue_flush_worker.get_indihomes_client", lambda: indihomes)
    monkeypatch.setattr("workers.queue_flush_worker.get_wati_client", lambda: wati)

    await queue_flush_worker.run_cycle()

    assert len(wati.sent_templates) == 0
    assert pending_queue.pending_count() == 1  # still there, not lost


@pytest.mark.asyncio
async def test_flush_drains_oldest_first(monkeypatch):
    monkeypatch.setattr("services.campaign_service.is_business_hours", lambda: True)

    pending_queue.enqueue(_make_lead(lead_id="1"), category="property_campaign")
    pending_queue.enqueue(_make_lead(lead_id="2"), category="property_campaign")

    wati = FakeWatiClient()
    indihomes = FakeIndihomesClient(project={"projectCode": "P1", "projectName": "Some Project"})
    monkeypatch.setattr("workers.queue_flush_worker.get_indihomes_client", lambda: indihomes)
    monkeypatch.setattr("workers.queue_flush_worker.get_wati_client", lambda: wati)

    await queue_flush_worker.run_cycle()

    assert len(wati.sent_templates) == 2
    assert pending_queue.pending_count() == 0


@pytest.mark.asyncio
async def test_flush_with_empty_queue_does_nothing(monkeypatch):
    wati = FakeWatiClient()
    indihomes = FakeIndihomesClient()
    monkeypatch.setattr("workers.queue_flush_worker.get_indihomes_client", lambda: indihomes)
    monkeypatch.setattr("workers.queue_flush_worker.get_wati_client", lambda: wati)

    await queue_flush_worker.run_cycle()  # must not raise

    assert len(wati.sent_templates) == 0
