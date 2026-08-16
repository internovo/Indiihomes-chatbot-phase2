"""Main orchestrator: coordinates lead processing, property lookup,
template creation, WATI messaging, and the CRM status update.

Two processing paths, chosen by workers/campaign_worker.py based on
lead_service.classify_lead() - data-presence based, not source-string
based (see that module's docstring for why):

  process_lead          - Property Campaign (has project_code/project_name)
                           get-new-leads -> property_service -> formatter
                           -> template_service -> wati_client -> update-leads-by-id

  process_generic_lead  - Generic Interest (name + phone only)
                           no project data at all - just sends a
                           "thanks for your interest" template. Once the
                           lead taps its Yes/Interested quick-reply
                           button, WATI triggers Phase 1's existing
                           qualification flow from its own start (see
                           the WATI-side Trigger Flow config on that
                           button, not anything in this codebase).

Both return a CampaignRecord with the same status vocabulary, so
workers/retry_worker.py can retry either one identically - it takes
the processor function as a parameter rather than hardcoding
process_lead, specifically so Generic Interest leads get the exact
same retry/backoff treatment as Property Campaign ones.

CRITICAL (fixed 4 Aug 2026): the CRM status update (update_lead) used
to be inside the same try/except as everything else, including the
WATI send. That meant if update_lead failed AFTER a successful send
(confirmed real: every update-leads-by-id call was 404ing, apparently
never actually verified end-to-end before that day), the WHOLE
function was treated as failed and queued for retry - and a retry
re-runs the entire function, including RE-SENDING the WATI template to
a real customer. Once a send has genuinely succeeded, nothing after it
should ever be able to trigger a resend. update_lead now has its own
inner try/except: if it fails, that's logged loudly (it's still a real
problem worth knowing about - the CRM won't show this lead as
contacted), but the record stays TEMPLATE_SENT and is never retried.
"""
from integrations.indihomes_client import IndihomesClient
from integrations.lead_routing_client import notify_lead_routing_best_effort
from integrations.wati_client import WatiClient
from business_hours import is_business_hours
from config import get_settings
from models.campaign import CampaignRecord
from models.lead import Lead
from models.notify import NotifyAdvisorRequest
from services import campaign_context, notify_service, property_service, template_service
from utils import lead_send_lock, meta_delivery_store, opted_out_store, pending_queue, sent_template_store
from utils.constants import CampaignStatus
from utils.logger import get_logger

logger = get_logger("campaign_service")


async def _update_crm_status_after_send(indihomes_client: IndihomesClient, lead: Lead) -> None:
    """Best-effort CRM status update AFTER a WATI send has already
    succeeded. Deliberately swallows its own exceptions (logging
    loudly instead) - see module docstring for why a failure here must
    never be allowed to trigger a retry, which would re-send the
    message that already went out."""
    try:
        await indihomes_client.update_lead(lead.id, {"status": CampaignStatus.TEMPLATE_SENT})
    except Exception as exc:  # noqa: BLE001 - see docstring; must never propagate
        logger.error(
            "Template already sent to lead %s, but the CRM status update failed (NOT retrying - "
            "a retry would re-send the template to this real customer): %s",
            lead.id, exc,
        )


async def process_lead(
    lead: Lead,
    indihomes_client: IndihomesClient,
    wati_client: WatiClient,
) -> CampaignRecord:
    """Processes a single Property Campaign lead end to end. Returns a
    CampaignRecord describing the outcome so the caller (worker or
    retry worker) can decide what to do next - it never raises, since
    one bad lead shouldn't stop a whole worker cycle."""
    record = CampaignRecord(lead_id=lead.id, phone=lead.phone)

    if opted_out_store.is_opted_out(lead.phone):
        # Checked FIRST, before project resolution or anything else - a
        # phone that told us to stop must never be contacted again by
        # this pipeline, regardless of what project this particular
        # lead record is about. See utils/opted_out_store.py.
        logger.info("Lead %s (phone=%s) is opted out - skipping", lead.id, lead.phone)
        record.status = CampaignStatus.OPTED_OUT
        return record

    if not lead.project_code and not lead.project_name:
        # Defensive safety net, not an expected path: lead_service.classify_lead
        # only ever routes a lead here when project_code or project_name is
        # already present, so this should be unreachable in normal operation.
        # Kept anyway - if a future classification change or a bad direct
        # call to process_lead() ever violates that guarantee, this is what
        # stops the original silent-loss bug (3 retries over ~80 min, then
        # abandoned with nobody told) from coming back, rather than trusting
        # the invariant to hold forever.
        logger.warning(
            "Lead %s has no project_code or project_name (source=%r) - notifying advisor instead of retrying",
            lead.id, lead.lead_source,
        )
        notify_service.notify_advisor(NotifyAdvisorRequest(
            phone=lead.phone,
            name=lead.name or "",
            reason="unresolved_project",
            lead_source=lead.lead_source,
        ))
        record.status = CampaignStatus.ADVISOR_NOTIFIED
        return record

    try:
        prop = await property_service.resolve_property(indihomes_client, lead)
        if prop is None:
            record.mark_failed("property not resolved", backoff_seconds=15 * 60)
            return record
        record.status = CampaignStatus.PROPERTY_RESOLVED

        # Remembered so routes/campaign.py's POST /property-detail can
        # resolve the right project later purely from phone, even if
        # the WATI flow calls back before a project_code contact
        # attribute is set on the contact.
        campaign_context.remember(lead.phone, prop.project_code)
        logger.info(
    "Remembering phone=%s project_code=%s project_name=%s",
    lead.phone,
    prop.project_code,
    prop.project_name,
)
        payload = template_service.build_template_payload(lead, prop)

        # See utils/lead_send_lock.py's module docstring for the exact
        # duplicate-send race this closes: campaign_worker's regular poll
        # and queue_flush_worker's daily cron can both reach this point
        # for the SAME lead_id (a queued-off-hours lead whose CRM status
        # was never updated still looks "new" to the regular poller).
        # Held for the ENTIRE check-through-mark_sent section, including
        # the awaited WATI send itself - not just the has_sent() read -
        # since the race is between the CHECK and the MARK, not just
        # around the check alone.
        async with lead_send_lock.guard(lead.id):
            if sent_template_store.has_sent(lead.id, payload["template_name"]):
                record.mark_sent()
                logger.warning(
                    "Skipping duplicate campaign template send for lead %s template %s - already recorded as sent",
                    lead.id, payload["template_name"],
                )
            elif not is_business_hours():
                # Resolved and ready to send, but outside 10 AM - 7 PM IST.
                # Queue instead of sending - see Indihomes_Business_Hours_Gating.docx
                # §3.3 and utils/pending_queue.py. Deliberately checked AFTER the
                # has_sent() idempotency check above (a lead already sent must
                # never be re-queued) and returns immediately, so nothing below
                # this branch - including _update_crm_status_after_send - runs
                # for an off-hours lead; the CRM must not show it as contacted
                # until it actually has been.
                pending_queue.enqueue(lead, category="property_campaign")
                record.status = CampaignStatus.QUEUED_OFF_HOURS
                logger.info("Lead %s queued off-hours for project %s", lead.id, prop.project_name)
                return record
            else:
                await wati_client.send_template(payload["phone"], payload["template_name"], payload["parameters"])
                sent_template_store.mark_sent(lead.id, payload["template_name"])
                # Tracks delivery status via routes/webhook.py's WATI status
                # callbacks (delivered/failed) - see utils/meta_delivery_store.py's
                # module docstring. This is a SEPARATE concept from sent_template_store
                # above: that one means "WATI accepted the send"; this one means
                # "did it actually reach the customer", which is what the 10 AM
                # Meta-restricted resend (workers/meta_resend_worker.py) keys off.
                meta_delivery_store.record_sent(lead, payload["template_name"], category="property_campaign")
                # Writes last_campaign_template onto the WATI contact so the
                # 24h follow-up Automation Rules (housing side) can filter on
                # it - see claude.md, "24h follow-up wiring". Best-effort and
                # deliberately outside the lock/critical section's error
                # handling: if this fails, the customer's message has ALREADY
                # sent successfully - a WATI-side attribute write failing must
                # never be treated as the send itself failing.
                try:
                    await wati_client.update_contact_attributes(
                        lead.phone, {"last_campaign_template": payload["template_name"]}
                    )
                except Exception as exc:  # noqa: BLE001 - never let this affect the send outcome above
                    logger.warning(
                        "Sent template to lead %s but failed to write last_campaign_template attribute: %s",
                        lead.id, exc,
                    )
                record.mark_sent()
                logger.info("Campaign template sent to lead %s for project %s", lead.id, prop.project_name)

    except Exception as exc:  # noqa: BLE001 - a failing lead must not crash the batch
        logger.error("Lead %s failed: %s", lead.id, exc)
        record.mark_failed(str(exc), backoff_seconds=5 * 60)
        return record

    # Past this point the message has DEFINITELY gone out - nothing
    # below can change record.status back to something retryable.

    # Phase 3: notify the resolved project's salesperson via
    # indihomes-lead-routing-service. Fired in BOTH the fresh-send and
    # duplicate-skip branches above (both reach here with record marked
    # sent) - the routing service is itself idempotent per lead_id, so
    # calling it again after a duplicate-skip is safe and is actually
    # the point: it catches the case where the customer's template went
    # out on a prior run but the salesperson notification silently
    # failed that time. Isolated in its own try/except inside
    # notify_lead_routing_best_effort() - same non-negotiable rule as
    # _update_crm_status_after_send below: nothing here may ever
    # re-trigger a resend of the CUSTOMER'S template.
    #
    # DISABLED as of 2026-08-12 - see config.py's lead_routing_meta_ads_
    # enabled docstring and claude.md, "Meta Ads salesperson notification
    # disabled", for the full story. Short version: this repo's Lead
    # model (models/lead.py) never captured configuration/budget from the
    # CRM at all, so every real Meta Ads salesperson notification sent so
    # far went out with "Looking For: -" and "Budget: -" - confirmed from
    # actual production WhatsApp messages. Re-enable via
    # LEAD_ROUTING_META_ADS_ENABLED=true only once that data is actually
    # captured and forwarded correctly.
    settings = get_settings()
    if settings.lead_routing_meta_ads_enabled:
        await notify_lead_routing_best_effort(lead, prop)
    else:
        logger.info(
            "Phase 3 salesperson notification skipped for lead %s (Meta Ads routing "
            "disabled pending configuration/budget field fix - see claude.md)",
            lead.id,
        )

    await _update_crm_status_after_send(indihomes_client, lead)
    return record


async def process_generic_lead(
    lead: Lead,
    indihomes_client: IndihomesClient,
    wati_client: WatiClient,
) -> CampaignRecord:
    """Processes a single Generic Interest lead (name + phone only, no
    project data - DIRECT, Meta Ads, 99 Acres, or anything else that
    doesn't carry project_code/project_name). Deliberately much
    simpler than process_lead: no property_service call, no
    campaign_context, no /property-detail relevance at all - just the
    opening template. Everything after the lead taps it is WATI's own
    flow, not this service's concern."""
    record = CampaignRecord(lead_id=lead.id, phone=lead.phone)

    if opted_out_store.is_opted_out(lead.phone):
        logger.info("Lead %s (phone=%s) is opted out - skipping", lead.id, lead.phone)
        record.status = CampaignStatus.OPTED_OUT
        return record

    try:
        settings = get_settings()
        template_name = settings.wati_generic_template_name
        parameters = [
            # "1" = the positional placeholder in the approved template's
            # body ({{1}}) - see formatter.template_parameters for why
            # this must be the position number, not a descriptive label.
            {"name": "1", "value": lead.name or "there"},
        ]

        # Same duplicate-send race as process_lead above - see
        # utils/lead_send_lock.py's module docstring.
        async with lead_send_lock.guard(lead.id):
            if sent_template_store.has_sent(lead.id, template_name):
                record.mark_sent()
                logger.warning(
                    "Skipping duplicate campaign template send for lead %s template %s - already recorded as sent",
                    lead.id, template_name,
                )
            elif not is_business_hours():
                # Same gate as process_lead's - see the comment there for the
                # full reasoning. No property to reference in the log line
                # here since Generic Interest leads never resolve one.
                pending_queue.enqueue(lead, category="generic_interest")
                record.status = CampaignStatus.QUEUED_OFF_HOURS
                logger.info("Lead %s queued off-hours (generic interest)", lead.id)
                return record
            else:
                await wati_client.send_template(lead.phone, template_name, parameters)
                sent_template_store.mark_sent(lead.id, template_name)
                # See the matching comment in process_lead above.
                meta_delivery_store.record_sent(lead, template_name, category="generic_interest")
                # See the matching comment + reasoning in process_lead above.
                try:
                    await wati_client.update_contact_attributes(
                        lead.phone, {"last_campaign_template": template_name}
                    )
                except Exception as exc:  # noqa: BLE001 - never let this affect the send outcome above
                    logger.warning(
                        "Sent template to lead %s but failed to write last_campaign_template attribute: %s",
                        lead.id, exc,
                    )
                record.mark_sent()
                logger.info("Generic-interest template sent to lead %s (source=%r)", lead.id, lead.lead_source)

    except Exception as exc:  # noqa: BLE001 - a failing lead must not crash the batch
        logger.error("Generic lead %s failed: %s", lead.id, exc)
        record.mark_failed(str(exc), backoff_seconds=5 * 60)
        return record


    await _update_crm_status_after_send(indihomes_client, lead)
    return record


async def process_batch(
    leads: list[Lead],
    indihomes_client: IndihomesClient,
    wati_client: WatiClient,
) -> list[CampaignRecord]:
    return [await process_lead(lead, indihomes_client, wati_client) for lead in leads]


async def process_generic_batch(
    leads: list[Lead],
    indihomes_client: IndihomesClient,
    wati_client: WatiClient,
) -> list[CampaignRecord]:
    return [await process_generic_lead(lead, indihomes_client, wati_client) for lead in leads]


