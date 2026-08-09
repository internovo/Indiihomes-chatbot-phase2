"""Tests for the Phase 2 campaign flow's free-text fallback:
services/campaign_intent_router.py, utils/opted_out_store.py, the
opt-out gate wired into services/campaign_service.py, and
routes/campaign.py's POST /interpret-message.

Run: pytest tests/test_campaign_intent_router.py -v

See services/campaign_intent_router.py's module docstring for the
honesty note this whole file inherits: these tests check the CODE
behaves correctly for the intents it claims to recognise, not that
the phrase lists match what a real lead (e.g. Monali) actually typed
- that transcript was never captured. Recalibrate the phrase lists
(and these tests alongside them) once real /interpret-message logs
exist.
"""
import pytest

from services import campaign_intent_router
from models.interpret import InterpretMessageRequest
from models.lead import Lead
from routes.campaign import interpret_message
from services import campaign_service
from utils import opted_out_store
from utils.constants import CampaignStatus


# --------------------------------------------------------------------------
# services/campaign_intent_router.py - direct classifier tests
# --------------------------------------------------------------------------

class TestClassify:
    def test_empty_text_is_none(self):
        assert campaign_intent_router.classify("") == {"intent": "none"}

    def test_stop_variants(self):
        for text in ["stop", "STOP", "please unsubscribe", "don't message me"]:
            assert campaign_intent_router.classify(text)["intent"] == "stop", text

    def test_stop_wins_over_advisor_phrase_in_same_message(self):
        out = campaign_intent_router.classify("stop messaging me, don't send an advisor either")
        assert out["intent"] == "stop"

    def test_advisor_variants(self):
        for text in ["can I talk to someone", "connect me to an agent", "call me"]:
            assert campaign_intent_router.classify(text)["intent"] == "talk_to_advisor", text

    def test_site_visit_variants(self):
        for text in ["I want to book a site visit", "can I visit the property", "book a visit"]:
            assert campaign_intent_router.classify(text)["intent"] == "site_visit", text

    def test_not_interested_variants(self):
        for text in ["not interested", "no thanks", "not right now"]:
            assert campaign_intent_router.classify(text)["intent"] == "not_interested", text

    def test_show_details_variants(self):
        for text in ["tell me more", "send details again", "more information please"]:
            assert campaign_intent_router.classify(text)["intent"] == "show_details", text

    def test_unrecognised_text_is_none(self):
        assert campaign_intent_router.classify("asdkjfh random gibberish")["intent"] == "none"


# --------------------------------------------------------------------------
# utils/opted_out_store.py - persistence round trip
# --------------------------------------------------------------------------

class TestOptedOutStore:
    def test_mark_and_check(self):
        assert not opted_out_store.is_opted_out("919900000001")
        opted_out_store.mark_opted_out("919900000001")
        assert opted_out_store.is_opted_out("919900000001")

    def test_idempotent(self):
        opted_out_store.mark_opted_out("919900000002")
        opted_out_store.mark_opted_out("919900000002")  # must not raise
        assert opted_out_store.is_opted_out("919900000002")

    def test_unknown_phone_is_not_opted_out(self):
        assert not opted_out_store.is_opted_out("919900000003")

    def test_empty_phone_is_safe(self):
        assert not opted_out_store.is_opted_out("")
        opted_out_store.mark_opted_out("")  # must not raise


# --------------------------------------------------------------------------
# services/campaign_service.py - opt-out gate wired into the send path
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


@pytest.mark.asyncio
async def test_process_lead_skips_opted_out_phone(monkeypatch):
    monkeypatch.setattr("services.campaign_service.is_business_hours", lambda: True)
    opted_out_store.mark_opted_out("919876543210")
    lead = Lead(_id="1", phone="919876543210", name="Test", leadSource="Housing.com",
                projectCode="P1", projectName="Some Project")
    indihomes = FakeIndihomesClient(project={"projectCode": "P1", "projectName": "Some Project"})
    wati = FakeWatiClient()

    record = await campaign_service.process_lead(lead, indihomes, wati)

    assert record.status == CampaignStatus.OPTED_OUT
    assert len(wati.sent_templates) == 0
    assert len(indihomes.updated_leads) == 0


@pytest.mark.asyncio
async def test_process_generic_lead_skips_opted_out_phone(monkeypatch):
    monkeypatch.setattr("services.campaign_service.is_business_hours", lambda: True)
    opted_out_store.mark_opted_out("919876543211")
    lead = Lead(_id="1", phone="919876543211", name="Test", leadSource="Meta Ads")
    indihomes = FakeIndihomesClient()
    wati = FakeWatiClient()

    record = await campaign_service.process_generic_lead(lead, indihomes, wati)

    assert record.status == CampaignStatus.OPTED_OUT
    assert len(wati.sent_templates) == 0


@pytest.mark.asyncio
async def test_non_opted_out_phone_sends_normally(monkeypatch):
    monkeypatch.setattr("services.campaign_service.is_business_hours", lambda: True)
    lead = Lead(_id="1", phone="919876543212", name="Test", leadSource="Housing.com",
                projectCode="P1", projectName="Some Project")
    indihomes = FakeIndihomesClient(project={"projectCode": "P1", "projectName": "Some Project"})
    wati = FakeWatiClient()

    record = await campaign_service.process_lead(lead, indihomes, wati)

    assert record.status == CampaignStatus.TEMPLATE_SENT
    assert len(wati.sent_templates) == 1


# --------------------------------------------------------------------------
# routes/campaign.py - POST /interpret-message end to end
# --------------------------------------------------------------------------

# --------------------------------------------------------------------------
# routes/campaign.py - POST /interpret-message, called directly as an async
# function (same convention every other test file in this project uses -
# see test_leads.py / test_notify.py - rather than TestClient(app), which
# would also spin up app.py's real AsyncIOScheduler lifespan and its 4
# background jobs unnecessarily for a unit test).
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def fake_email_client(monkeypatch):
    """talk_to_advisor / site_visit / not_interested all call
    notify_service.notify_advisor() internally - never let that hit
    the real Brevo API from a test."""
    class FakeEmail:
        def send(self, to, cc, subject, body):
            return True
    monkeypatch.setattr("services.notify_service.get_email_client", lambda: FakeEmail())


@pytest.mark.asyncio
async def test_interpret_message_stop_marks_opted_out():
    resp = await interpret_message(InterpretMessageRequest(
        phone="919900000010", message="please stop messaging me",
    ))
    assert resp.intent == "stop"
    assert resp.is_global == "yes"
    assert opted_out_store.is_opted_out("919900000010")


@pytest.mark.asyncio
async def test_interpret_message_talk_to_advisor():
    resp = await interpret_message(InterpretMessageRequest(
        phone="919900000011", message="can someone call me please",
    ))
    assert resp.intent == "talk_to_advisor"
    assert resp.handled == "yes"


@pytest.mark.asyncio
async def test_interpret_message_none_intent():
    resp = await interpret_message(InterpretMessageRequest(
        phone="919900000012", message="asdkjfh nonsense",
    ))
    assert resp.intent == "none"
    assert resp.is_global == "no"


@pytest.mark.asyncio
async def test_interpret_message_never_errors_on_minimal_body():
    resp = await interpret_message(InterpretMessageRequest(phone="919900000013"))
    assert resp.intent == "none"


@pytest.mark.asyncio
async def test_interpret_message_site_visit_notifies_advisor_not_a_booking():
    resp = await interpret_message(InterpretMessageRequest(
        phone="919900000014", message="I'd like to book a site visit",
        projectCode="P1", projectName="Some Project",
    ))
    assert resp.intent == "site_visit"
    assert resp.handled == "yes"


@pytest.mark.asyncio
async def test_interpret_message_not_interested():
    resp = await interpret_message(InterpretMessageRequest(
        phone="919900000015", message="not interested, thanks",
    ))
    assert resp.intent == "not_interested"
    assert resp.handled == "yes"
