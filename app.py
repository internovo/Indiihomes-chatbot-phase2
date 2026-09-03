"""FastAPI entrypoint.
Flow: Start FastAPI -> Register Routes -> Start Scheduler -> Ready
"""
from contextlib import asynccontextmanager

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI

from config import get_settings
from integrations.indihomes_client import get_indihomes_client
from integrations.wati_client import get_wati_client
from business_hours import IST_NAME
from routes import admin, campaign, health, webhook
from utils.logger import get_logger
from workers import campaign_worker, cleanup_worker, meta_resend_worker, queue_flush_worker, retry_worker

logger = get_logger("app")
scheduler = AsyncIOScheduler()


async def _run_campaign_cycle():
    try:
        await campaign_worker.run_cycle()
    except Exception:  # noqa: BLE001 - a worker cycle must never kill the scheduler
        logger.exception("campaign_worker cycle raised an unhandled error")


async def _run_retry_cycle():
    try:
        await retry_worker.run_cycle(get_indihomes_client(), get_wati_client())
    except Exception:  # noqa: BLE001
        logger.exception("retry_worker cycle raised an unhandled error")


def _run_cleanup_cycle():
    try:
        cleanup_worker.run_cycle()
    except Exception:  # noqa: BLE001
        logger.exception("cleanup_worker cycle raised an unhandled error")


async def _run_queue_flush_cycle():
    try:
        await queue_flush_worker.run_cycle()
    except Exception:  # noqa: BLE001 - one bad flush run must never kill the scheduler
        logger.exception("queue_flush_worker cycle raised an unhandled error")


async def _run_meta_resend_cycle():
    try:
        await meta_resend_worker.run_cycle(get_indihomes_client(), get_wati_client())
    except Exception:  # noqa: BLE001 - one bad resend run must never kill the scheduler
        logger.exception("meta_resend_worker cycle raised an unhandled error")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    scheduler.add_job(_run_campaign_cycle, "interval", seconds=settings.poll_interval_seconds, id="campaign_worker")
    scheduler.add_job(_run_retry_cycle, "interval", seconds=settings.retry_worker_interval_seconds, id="retry_worker")
    scheduler.add_job(_run_cleanup_cycle, "interval", seconds=settings.cleanup_interval_seconds, id="cleanup_worker")
    # Business-hours gating (see business_hours.py, services/campaign_service.py,
    # utils/pending_queue.py): drains leads queued overnight once a day, at
    # business open. A "cron" trigger (not "interval") because this must fire
    # at a specific wall-clock time in IST regardless of when the process
    # started, unlike the interval-based workers above.
    scheduler.add_job(
        _run_queue_flush_cycle, "cron",
        hour=settings.queue_flush_hour_ist, minute=settings.queue_flush_minute_ist,
        timezone=IST_NAME, id="queue_flush_worker",
    )
    # Meta-restricted resend (see utils/meta_delivery_store.py,
    # routes/webhook.py, workers/meta_resend_worker.py) - resends leads
    # Meta marked UNDELIVERED the previous day, once, at 10 AM IST.
    # Same "cron" reasoning as queue_flush_worker above: must fire at a
    # specific wall-clock time in IST, not N seconds after process start.
    scheduler.add_job(
        _run_meta_resend_cycle, "cron",
        hour=settings.meta_resend_hour_ist, minute=settings.meta_resend_minute_ist,
        timezone=IST_NAME, id="meta_resend_worker",
    )
    scheduler.start()
    logger.info("Phase 2 Campaign Service started (env=%s)", settings.environment)
    yield
    scheduler.shutdown(wait=False)
    logger.info("Phase 2 Campaign Service stopped")


app = FastAPI(title="IndiHomes Phase 2 Campaign Service", lifespan=lifespan)

app.include_router(health.router)
app.include_router(webhook.router)
app.include_router(campaign.router)
app.include_router(admin.router)
