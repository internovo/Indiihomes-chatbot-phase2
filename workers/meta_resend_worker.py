"""Daily 10 AM IST resend for leads whose campaign template was sent
but Meta marked UNDELIVERED (see utils/meta_delivery_store.py and
routes/webhook.py). Business rule, confirmed with the manager:

  - Resend the SAME template, next day at 10 AM IST.
  - ONLY if it was actually undelivered (a templateMessageFailed
    webhook was received) - a lead that got the message with double
    ticks (sentMessageDELIVERED_v2/READ) must NEVER be resent.
  - Exactly ONE resend attempt - this is not a repeating retry loop.

Deliberately does NOT reuse campaign_service.process_lead /
process_generic_lead the way workers/queue_flush_worker.py does for
the off-hours queue. Those two functions gate on
sent_template_store.has_sent() FIRST - and by definition every lead in
this queue already has has_sent() == True (that's how it got a
delivery-status record to fail against in the first place), so running
through them would just skip every single entry. This worker owns its
own minimal send path instead, reusing only the pieces that are still
correct to reuse: the per-lead lock (same duplicate-send race this
service already guards against elsewhere) and the same parameter-
building services (template_service / formatter) so the resent message
is byte-for-byte the same template call as the original.
"""
from integrations.indihomes_client import IndihomesClient
from integrations.os_events_client import emit_best_effort as emit_os_event
from integrations.wati_client import WatiClient
from services import property_service, template_service
from utils import lead_send_lock, meta_delivery_store
from utils.logger import get_logger

logger = get_logger("meta_resend_worker")


async def _resend_property_campaign(entry: dict, indihomes_client: IndihomesClient, wati_client: WatiClient) -> bool:
    lead = meta_delivery_store.to_lead(entry)
    # Re-resolve the property fresh rather than trusting an overnight-old
    # snapshot - same reasoning as queue_flush_worker.py's off-hours
    # flush: a stale property_service.Property (price/availability
    # changed overnight) must never be sent as if current.
    prop = await property_service.resolve_property(indihomes_client, lead)
    if prop is None:
        logger.error(
            "Lead %s: could not re-resolve property for 10 AM Meta-restricted resend - skipping this run.",
            lead.id,
        )
        return False
    payload = template_service.build_template_payload(lead, prop)
    await wati_client.send_template(payload["phone"], payload["template_name"], payload["parameters"])
    return True


async def _resend_generic(entry: dict, wati_client: WatiClient) -> bool:
    lead = meta_delivery_store.to_lead(entry)
    template_name = entry["template_name"]
    parameters = [{"name": "1", "value": lead.name or "there"}]
    await wati_client.send_template(lead.phone, template_name, parameters)
    return True


async def run_cycle(indihomes_client: IndihomesClient, wati_client: WatiClient) -> None:
    failed_entries = meta_delivery_store.load_failed()
    if not failed_entries:
        # Explicit heartbeat, not silence - previously this function
        # returned with NO log line at all when there was nothing to do,
        # which made "the cron job didn't fire" and "it fired and found
        # nothing" indistinguishable in the logs. Added after exactly
        # that ambiguity came up while verifying this worker for real
        # against production leads on 16 Aug 2026.
        logger.info("10 AM IST Meta-restricted resend: cycle ran, 0 leads currently marked undelivered.")
        return

    logger.info("10 AM IST Meta-restricted resend: %d lead(s) marked undelivered yesterday", len(failed_entries))

    resent = skipped_now_delivered = errors = 0

    for entry in failed_entries:
        lead_id = entry["lead_id"]
        template_name = entry["template_name"]
        category = entry.get("category", "generic_interest")

        async with lead_send_lock.guard(lead_id):
            # Final re-check immediately before resending - a late
            # DELIVERED webhook may have landed between the queue read
            # above and now (see meta_delivery_store.mark_delivered).
            # This is the literal enforcement of "if the lead has
            # received the message with double ticks, no resend."
            current_status = meta_delivery_store.get_current_status(lead_id, template_name)
            if current_status != meta_delivery_store.DeliveryStatus.FAILED:
                logger.info(
                    "Lead %s template %s status changed to %s before the 10 AM resend ran - skipping resend.",
                    lead_id, template_name, current_status,
                )
                skipped_now_delivered += 1
                continue

            try:
                if category == "property_campaign":
                    sent_ok = await _resend_property_campaign(entry, indihomes_client, wati_client)
                else:
                    sent_ok = await _resend_generic(entry, wati_client)
            except Exception as exc:  # noqa: BLE001 - one bad resend must not stop the rest of the batch
                logger.error("10 AM resend failed for lead %s template %s: %s", lead_id, template_name, exc)
                sent_ok = False
                errors += 1

            # Marked RESENT regardless of outcome - see mark_resent's
            # docstring: this is a one-time attempt, not a retry loop.
            # A resend that itself fails is a job for a human (Meta
            # blocked it twice), not another automatic attempt.
            meta_delivery_store.mark_resent(lead_id, template_name)
            if sent_ok:
                resent += 1
                logger.info("Resent template %s to lead %s at 10 AM IST (previously undelivered)", template_name, lead_id)
                # Lead-events: only fired on an actual successful resend -
                # a resend attempt that itself failed has nothing new to
                # tell indihomes-os beyond the 'failed' checkpoint already
                # emitted by routes/webhook.py when the ORIGINAL send failed.
                try:
                    await emit_os_event(entry.get("phone", ""), "resent", {
                        "template_name": template_name,
                    }, idempotency_key=f"{lead_id}:resent:{template_name}")
                except Exception as exc:  # noqa: BLE001 - must never affect the resend outcome above
                    logger.error("os_events_client resent emit failed for lead %s: %s", lead_id, exc)

    logger.info(
        "10 AM Meta-restricted resend complete: %d resent, %d skipped (delivered before resend ran), %d errors",
        resent, skipped_now_delivered, errors,
    )
