"""Health endpoint for Railway health checks and monitoring, plus the
/debug/pipeline snapshot added 3 Sep 2026.

Why /debug/pipeline exists: this service ran for five days looking
completely healthy (200s on /health, no crashes, no unhandled
exceptions, checkpoint advancing) while sending zero campaign
templates. Every question worth asking in that state - what is the
checkpoint right now, how many templates has it ever sent, what
happened to the last few leads - was only answerable from Railway log
history, which serves roughly the last 1000 lines (about three hours
at a 45-second poll). By the time anyone noticed, the evidence was
gone. This endpoint answers all of it in one call, from live state.
"""
from fastapi import APIRouter

from business_hours import is_business_hours
from config import get_settings
from integrations import lead_routing_client
from utils import checkpoint, pending_queue, sent_template_store
from workers import campaign_worker, retry_worker

router = APIRouter()


@router.get("/health")
async def health():
    settings = get_settings()
    if not lead_routing_client.is_configured():
        lead_routing_status = "NOT CONFIGURED (Phase 3 hook is a no-op until LEAD_ROUTING_URL/LEAD_ROUTING_SHARED_SECRET are set)"
    elif settings.lead_routing_dry_run:
        lead_routing_status = "configured, DRY-RUN"
    else:
        lead_routing_status = "configured, LIVE"
    return {
        "status": "ok",
        "pending_retries": retry_worker.pending_count(),
        "business_hours": "open" if is_business_hours() else "closed",
        "pending_off_hours_queue": pending_queue.pending_count(),
        "oldest_queued_at": pending_queue.oldest_queued_at(),
        "lead_routing": lead_routing_status,
        # The two fields that would have caught the 29 Aug - 2 Sep silent
        # gap on day one. A checkpoint of null means state/ didn't
        # persist and leads are being skipped; a sent-template count that
        # stops climbing means the pipeline has stopped delivering,
        # whatever else looks fine.
        "checkpoint": checkpoint.peek(),
        "templates_sent_total": sent_template_store.sent_count(),
    }


@router.get("/debug/pipeline")
async def debug_pipeline():
    """What actually happened to the most recent leads, newest first.

    Unauthenticated, same trust boundary as every other endpoint in this
    service (see routes/campaign.py's own note). It exposes lead phone
    numbers, so put it behind auth before this service is ever reachable
    from anywhere but Railway's own URL.
    """
    return {
        "checkpoint": checkpoint.peek(),
        "business_hours": "open" if is_business_hours() else "closed",
        "pending_retries": retry_worker.pending_count(),
        "pending_off_hours_queue": pending_queue.pending_count(),
        "templates_sent_total": sent_template_store.sent_count(),
        "recent_outcomes": campaign_worker.recent_outcomes(),
    }
