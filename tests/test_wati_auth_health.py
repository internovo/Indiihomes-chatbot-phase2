"""Regression tests for the 3 Sep 2026 WATI-credential visibility fix.

The incident: the WATI access token expired ~28 Aug 2026. Every
sendTemplateMessage returned 401, which the pipeline treated as an
ordinary transient failure - retried, abandoned, advisor-emailed, per
lead, for five days - while /health kept saying "ok". These tests lock
in the two things that make that state visible instead: an ERROR-level
log naming the real cause, and a wati_auth field on /health.
"""
import asyncio
import unittest

import httpx

from integrations import wati_client


def _client_with_response(status_code: int, payload=None):
    """A WatiClient whose HTTP layer is a stub returning one status."""
    client = wati_client.WatiClient()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=payload if payload is not None else {"result": True})

    async def _fake_client():
        return httpx.AsyncClient(
            base_url="https://wati.test",
            transport=httpx.MockTransport(handler),
        )

    client._client = _fake_client
    return client


class WatiAuthStatusTests(unittest.TestCase):
    def setUp(self):
        wati_client._auth_state.update({"status": "unknown", "detail": "", "at": None})

    def test_starts_unknown_before_any_send(self):
        """A freshly restarted service has proven nothing either way."""
        self.assertEqual(wati_client.auth_status()["status"], "unknown")

    def test_successful_send_records_ok(self):
        client = _client_with_response(200)
        asyncio.run(client.send_template("919999999999", "campaign_property_intro", []))
        self.assertEqual(wati_client.auth_status()["status"], "ok")

    def test_401_records_rejected_and_logs_at_error(self):
        client = _client_with_response(401, {"error": "unauthorized"})
        with self.assertLogs("wati_client", level="ERROR") as logs:
            with self.assertRaises(httpx.HTTPStatusError):
                # with_retry re-raises after exhausting its attempts;
                # base_delay is patched out so the test doesn't sleep.
                asyncio.run(self._no_sleep(client))
        state = wati_client.auth_status()
        self.assertEqual(state["status"], "REJECTED")
        self.assertIn("401", state["detail"])
        self.assertIsNotNone(state["at"])
        joined = " ".join(logs.output)
        self.assertIn("REJECTED THIS SERVICE'S CREDENTIALS", joined)
        self.assertIn("NOT a transient failure", joined)

    def test_403_is_treated_the_same_as_401(self):
        client = _client_with_response(403)
        with self.assertRaises(httpx.HTTPStatusError):
            asyncio.run(self._no_sleep(client))
        self.assertEqual(wati_client.auth_status()["status"], "REJECTED")

    def test_a_500_is_not_reported_as_an_auth_problem(self):
        """A server error is a genuine transient failure - it must not
        send anyone chasing a token that is actually fine."""
        client = _client_with_response(500)
        with self.assertRaises(httpx.HTTPStatusError):
            asyncio.run(self._no_sleep(client))
        self.assertEqual(wati_client.auth_status()["status"], "unknown")

    async def _no_sleep(self, client):
        """Runs a send with the retry decorator's backoff sleep removed,
        so a 3-attempt failure path doesn't cost 6 real seconds."""
        import utils.retry as retry_module
        original = asyncio.sleep

        async def instant(_seconds):
            await original(0)

        retry_module.asyncio.sleep = instant
        try:
            return await client.send_template("919999999999", "campaign_property_intro", [])
        finally:
            retry_module.asyncio.sleep = original


class HealthExposesAuthStateTests(unittest.TestCase):
    def setUp(self):
        wati_client._auth_state.update({"status": "unknown", "detail": "", "at": None})

    def test_health_surfaces_wati_auth(self):
        from routes import health
        body = asyncio.run(health.health())
        self.assertIn("wati_auth", body)
        self.assertEqual(body["wati_auth"]["status"], "unknown")

        wati_client._note_auth(False, "HTTP 401 from sendTemplateMessage")
        body = asyncio.run(health.health())
        self.assertEqual(body["wati_auth"]["status"], "REJECTED")

    def test_debug_pipeline_surfaces_wati_auth_too(self):
        from routes import health
        body = asyncio.run(health.debug_pipeline())
        self.assertIn("wati_auth", body)


if __name__ == "__main__":
    unittest.main()
