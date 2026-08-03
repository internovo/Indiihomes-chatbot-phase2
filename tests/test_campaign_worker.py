"""Tests for the checkpoint-aware campaign_worker.run_cycle flow:
loads checkpoint -> fetches with afterDate -> classifies into Property
Campaign / Generic Interest -> processes each -> advances checkpoint
using the newest lead timestamp in the batch.

Also covers:
- the silent-lead-loss fix: any lead that exhausts every retry attempt
  notifies the advisor instead of just vanishing
  (retry_worker.queue_for_retry)
- the Property Campaign / Generic Interest / Ignored split, classified
  by data presence rather than source string (see lead_service.py) -
  Meta Ads/anything-without-project-data leads get the generic
  template, but share the exact same retry queue/backoff as Property
  Campaign leads (retry_worker is parameterized by which processor to
  retry with, rather than hardcoding one)

Note: campaign_service.process_lead's own "no project data" advisor-notify
safety net (CampaignStatus.ADVISOR_NOTIFIED) is tested directly in
test_templates.py, calling process_lead() itself - not exercised here,
since the classifier now guarantees a lead never reaches process_lead
without project data in the first place (a Housing.com lead with no
project data correctly routes to the Generic Interest path instead -
see test_run_cycle_routes_no_project_data_lead_to_generic_regardless_of_source
below).

IMPORTANT: notify_advisor() isn't given a fake email client by
campaign_service/retry_worker (those call sites use the real one on
purpose, in production). So every test in this file patches
services.notify_service.get_email_client to a fake - otherwise
running this test file would attempt a real Brevo API call using
whatever's in .env.
"""
import pytest

from models.lead import Lead
from models.campaign import CampaignRecord
from services import campaign_service
from utils import checkpoint
from utils.constants import CampaignStatus
from workers import campaign_worker, retry_worker


class FakeEmailClient:
    def __init__(self):
        self.sent = []  # list of (to, cc, subject, body)

    def send(self, to, cc, subject, body):
        self.sent.append((to, cc, subject, body))
        return True


@pytest.fixture(autouse=True)
def fake_email_client(monkeypatch):
    """Prevents any real Brevo call from these tests - see module
    docstring. Returns the fake so tests can assert on what was sent."""
    fake = FakeEmailClient()
    monkeypatch.setattr("services.notify_service.get_email_client", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def temp_checkpoint(tmp_path, monkeypatch):
    fake_path = tmp_path / "checkpoint.json"
    monkeypatch.setattr(checkpoint, "_CHECKPOINT_PATH", str(fake_path))
    yield fake_path


@pytest.fixture(autouse=True)
def clear_retry_queue():
    retry_worker._queue.clear()
    yield
    retry_worker._queue.clear()


class FakeIndihomesClient:
    def __init__(self, leads, project=None):
        self._leads = leads
        self._project = project
        self.requested_after_date = None
        self.updated_leads = []

    async def get_new_leads(self, after_date):
        self.requested_after_date = after_date
        return self._leads

    async def fetch_project(self, project_id):
        return self._project

    async def fetch_project_by_name(self, project_name):
        return self._project

    async def update_lead(self, lead_id, payload):
        self.updated_leads.append((lead_id, payload))
        return {"ok": True}


class FakeWatiClient:
    def __init__(self):
        self.sent_templates = []  # list of (phone, template_name, parameters)

    async def send_template(self, phone, template_name, parameters):
        self.sent_templates.append((phone, template_name, parameters))
        return {"result": "success"}


@pytest.fixture(autouse=True)
def patch_clients(monkeypatch):
    """Lets each test inject its own fake clients via
    monkeypatch on the getters campaign_worker actually calls."""
    holder = {}

    def _set(indihomes=None, wati=None):
        if indihomes is not None:
            monkeypatch.setattr("workers.campaign_worker.get_indihomes_client", lambda: indihomes)
        if wati is not None:
            monkeypatch.setattr("workers.campaign_worker.get_wati_client", lambda: wati)
        holder["indihomes"] = indihomes
        holder["wati"] = wati

    return _set


@pytest.mark.asyncio
async def test_run_cycle_passes_checkpoint_as_after_date(patch_clients):
    checkpoint.save_checkpoint("2026-07-30T10:00:00.000Z")
    indihomes = FakeIndihomesClient(leads=[])
    patch_clients(indihomes=indihomes, wati=FakeWatiClient())

    await campaign_worker.run_cycle()

    assert indihomes.requested_after_date == "2026-07-30T10:00:00.000Z"


@pytest.mark.asyncio
async def test_run_cycle_advances_checkpoint_to_newest_lead(patch_clients):
    leads = [
        {"_id": "1", "phone": "9876543210", "lead_source": "WhatsApp Bot", "projectCode": "INV_GE_901",
         "leadDate": "2026-07-30T11:00:00.000Z"},  # ignored despite having project data
        {"_id": "2", "phone": "9876543211", "lead_source": "Housing.com", "projectCode": "ETH-ORO-01",
         "leadDate": "2026-07-30T12:30:00.000Z"},
    ]
    indihomes = FakeIndihomesClient(leads=leads, project={"projectCode": "ETH-ORO-01", "projectName": "Ethics Orovia"})
    patch_clients(indihomes=indihomes, wati=FakeWatiClient())

    await campaign_worker.run_cycle()

    assert checkpoint.get_after_date() == "2026-07-30T12:30:00.000Z"
    # Ignored-category lead (WhatsApp Bot) should not be sent a
    # template / updated - only the Housing.com one.
    assert len(indihomes.updated_leads) == 1
    assert indihomes.updated_leads[0][0] == "2"


@pytest.mark.asyncio
async def test_run_cycle_does_not_advance_checkpoint_on_empty_batch(patch_clients):
    checkpoint.save_checkpoint("2026-07-30T10:00:00.000Z")
    indihomes = FakeIndihomesClient(leads=[])
    patch_clients(indihomes=indihomes, wati=FakeWatiClient())

    await campaign_worker.run_cycle()

    assert checkpoint.get_after_date() == "2026-07-30T10:00:00.000Z"


@pytest.mark.asyncio
async def test_run_cycle_queues_failed_lead_for_retry_but_still_advances_checkpoint(patch_clients):
    leads = [
        {"_id": "1", "phone": "9876543210", "lead_source": "Housing.com", "projectCode": "MISSING",
         "leadDate": "2026-07-30T13:00:00.000Z"},
    ]
    indihomes = FakeIndihomesClient(leads=leads, project=None)  # property never resolves -> failure
    patch_clients(indihomes=indihomes, wati=FakeWatiClient())

    await campaign_worker.run_cycle()

    assert checkpoint.get_after_date() == "2026-07-30T13:00:00.000Z"
    assert retry_worker.pending_count() == 1


@pytest.mark.asyncio
async def test_run_cycle_routes_no_project_data_lead_to_generic_regardless_of_source(patch_clients):
    """The actual 31 Jul finding: a Housing.com lead with no project
    data (confirmed real, e.g. "Amanjit Ahluwalia") is NOT a data-
    quality problem to alert on anymore - it correctly routes to the
    Generic Interest / "thanks for your interest" path, same as a
    Meta Ads lead would (but NOT the same as a DIRECT lead - see
    test_run_cycle_ignores_direct_and_whatsapp_bot_leads)."""
    leads = [
        {"_id": "1", "phone": "9876543210", "name": "Amanjit Ahluwalia",
         "lead_source": "Housing.com",  # no projectCode, no projectName
         "leadDate": "2026-07-31T13:00:00.000Z"},
    ]
    indihomes = FakeIndihomesClient(leads=leads, project=None)
    wati = FakeWatiClient()
    patch_clients(indihomes=indihomes, wati=wati)

    await campaign_worker.run_cycle()

    assert len(wati.sent_templates) == 1
    assert wati.sent_templates[0][1] == "campaign_generic_intro"
    assert indihomes.updated_leads[0][1] == {"status": "template_sent"}


@pytest.mark.asyncio
async def test_run_cycle_sends_generic_template_for_meta_ads_leads(patch_clients):
    """Generic Interest leads - Meta Ads creatives with no project
    data - get the generic "thanks for your interest" template, never
    touching property_service/campaign_context. Parameter "name" is
    "1" (positional), matching the approved template's {{1}}
    placeholder - see tests/test_templates.py's
    test_build_template_payload_shape docstring for why."""
    leads = [
        {"_id": "1", "phone": "9876543210", "name": "Priya",
         "lead_source": "Ethics Orovia EOI Malad W v2 2907",
         "leadDate": "2026-07-31T13:00:00.000Z"},
    ]
    indihomes = FakeIndihomesClient(leads=leads, project=None)
    wati = FakeWatiClient()
    patch_clients(indihomes=indihomes, wati=wati)

    await campaign_worker.run_cycle()

    assert len(wati.sent_templates) == 1
    phone, template_name, parameters = wati.sent_templates[0]
    assert phone == "919876543210"
    assert template_name == "campaign_generic_intro"
    assert parameters == [{"name": "1", "value": "Priya"}]
    assert len(indihomes.updated_leads) == 1
    assert indihomes.updated_leads[0][1] == {"status": "template_sent"}


@pytest.mark.asyncio
async def test_run_cycle_ignores_direct_and_whatsapp_bot_leads(patch_clients):
    """The 31 Jul diagram-driven fix: DIRECT (WhatsApp icon on the
    website) and WhatsApp Bot leads have both already messaged first
    and are handled entirely by Phase 1's own flow - sending either a
    Phase 2 template would be a redundant re-contact. Neither should
    be touched at all, regardless of whether they happen to carry
    project data."""
    leads = [
        {"_id": "1", "phone": "9876543210", "name": "Macson Rodrigues",
         "lead_source": "DIRECT", "leadDate": "2026-07-31T13:00:00.000Z"},
        {"_id": "2", "phone": "9876543211", "name": "Someone Else",
         "lead_source": "WhatsApp Bot", "projectCode": "INV_GE_901",
         "leadDate": "2026-07-31T13:01:00.000Z"},
    ]
    indihomes = FakeIndihomesClient(leads=leads, project=None)
    wati = FakeWatiClient()
    patch_clients(indihomes=indihomes, wati=wati)

    await campaign_worker.run_cycle()

    assert len(wati.sent_templates) == 0
    assert len(indihomes.updated_leads) == 0
    # Still classified as "seen" for checkpoint purposes even though ignored.
    assert checkpoint.get_after_date() == "2026-07-31T13:01:00.000Z"


@pytest.mark.asyncio
async def test_retry_worker_notifies_advisor_when_a_lead_is_finally_abandoned(fake_email_client):
    """A lead that DID have project data but still failed every retry
    used to just disappear when abandoned. Now it emails the advisor
    first - tested directly against queue_for_retry rather than
    simulating 3 full retry cycles."""
    lead = Lead(_id="1", phone="9876543210", name="Test User",
                lead_source="Housing.com", projectCode="SOME-CODE", projectName="Some Project")
    record = CampaignRecord(lead_id="1", phone="9876543210")
    record.attempts = 4  # already exceeded MAX_RETRY_ATTEMPTS (3)

    retry_worker.queue_for_retry(lead, record, campaign_service.process_lead)

    assert record.status == CampaignStatus.ABANDONED
    assert retry_worker.pending_count() == 0
    assert len(fake_email_client.sent) == 1
    _, _, _, body = fake_email_client.sent[0]
    assert "Some Project" in body
    assert "couldn't be processed after all retries" in body


@pytest.mark.asyncio
async def test_retry_worker_retries_generic_interest_leads_with_the_generic_processor():
    """The parameterization point of this whole refactor: a Generic
    Interest lead queued for retry must be retried with
    process_generic_lead, not process_lead - otherwise a retry would
    incorrectly attempt property resolution on a lead that was never
    supposed to have any."""
    lead = Lead(_id="1", phone="9876543210", name="Priya", lead_source="Ethics Orovia EOI Malad W v2 2907")
    record = CampaignRecord(lead_id="1", phone="9876543210")
    record.mark_failed("transient failure", backoff_seconds=0)  # already due
    retry_worker.queue_for_retry(lead, record, campaign_service.process_generic_lead)

    indihomes = FakeIndihomesClient(leads=[])
    wati = FakeWatiClient()
    await retry_worker.run_cycle(indihomes, wati)

    assert retry_worker.pending_count() == 0
    assert len(wati.sent_templates) == 1
    assert wati.sent_templates[0][1] == "campaign_generic_intro"
