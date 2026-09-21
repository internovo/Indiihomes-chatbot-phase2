"""A template send WATI refuses inside a 200 must not be recorded as sent.

The bug: send_template() returned resp.json() without reading it, and
campaign_service calls sent_template_store.mark_sent() on the very next
line. So a refusal like

    HTTP 200  {"result": false, "info": "Check your template, it cannot
               have typos or blank text"}

marked the lead as sent permanently. has_sent() then skips it on every
future cycle, and because no message was ever created WATI never sends a
delivery-status webhook either - so meta_resend_worker's 10 AM sweep
never sees it. The lead is silently never contacted.

That exact body came back on every v1 template send in Aug 2026 (see
integrations/wati_client.py's module docstring), so this is a failure
mode this system has actually hit.

Found 2026-09-21 while fixing the identical defect in V1 (commit
a54457c in Indihomes-chatbot-V1).
"""
import asyncio
import unittest

import httpx

from integrations import wati_client
from integrations.wati_client import WatiSendRejected, _body_says_ok


def _client_returning(payload, status_code=200):
    client = wati_client.WatiClient()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload)

    async def _fake_client():
        return httpx.AsyncClient(base_url="https://wati.test",
                                 transport=httpx.MockTransport(handler))

    client._client = _fake_client
    return client


class BodySaysOkTests(unittest.TestCase):
    def test_all_real_success_shapes_pass(self):
        for payload in ({"result": True},
                        {"result": "success"},
                        {"ok": True, "errors": [], "message": {"whatsappMessageId": "wamid.X"}}):
            self.assertTrue(_body_says_ok(payload)[0], payload)

    def test_explicit_refusal_fails_and_keeps_info(self):
        ok, info = _body_says_ok(
            {"result": False, "info": "Check your template, it cannot have typos or blank text"})
        self.assertFalse(ok)
        self.assertIn("typos", info)

    def test_v2_error_field_is_surfaced(self):
        ok, info = _body_says_ok({"result": False, "error": "template not approved"})
        self.assertFalse(ok)
        self.assertIn("template not approved", info)

    def test_unrecognised_body_counts_as_success(self):
        """Never turn real deliveries into permanent failures over a shape change."""
        for payload in ({}, {"whatever": 1}, [], "nope", None):
            self.assertTrue(_body_says_ok(payload)[0], payload)


class SendTemplateRejectionTests(unittest.TestCase):
    def setUp(self):
        wati_client._auth_state.update({"status": "unknown", "detail": "", "at": None})

    def test_refusal_raises_so_the_lead_is_not_marked_sent(self):
        client = _client_returning(
            {"result": False, "info": "Check your template, it cannot have typos or blank text"})
        with self.assertRaises(WatiSendRejected) as ctx:
            asyncio.run(client.send_template("919999999999", "campaign_property_intro", []))
        self.assertIn("typos", str(ctx.exception))

    def test_successful_send_still_returns_the_body(self):
        payload = {"result": "success", "message": {"whatsappMessageId": "wamid.OK"}}
        out = asyncio.run(
            _client_returning(payload).send_template("919999999999", "campaign_property_intro", []))
        self.assertEqual(out, payload)

    def test_boolean_true_result_still_succeeds(self):
        """The shape the pre-existing test-suite stub uses - must not regress."""
        out = asyncio.run(
            _client_returning({"result": True}).send_template("919999999999", "t", []))
        self.assertEqual(out, {"result": True})

    def test_refusal_is_not_treated_as_an_auth_failure(self):
        """A refused send says nothing about the credentials - /health's
        wati_auth must not start crying 'REJECTED' over a bad template."""
        client = _client_returning({"result": False, "info": "bad template"})
        with self.assertRaises(WatiSendRejected):
            asyncio.run(client.send_template("919999999999", "t", []))
        self.assertEqual(wati_client.auth_status()["status"], "ok")


if __name__ == "__main__":
    unittest.main()
