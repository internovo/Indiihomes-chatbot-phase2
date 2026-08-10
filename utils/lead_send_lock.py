"""Per-lead-id async lock, closing a real duplicate-send race between
workers/campaign_worker.py's regular poll and workers/queue_flush_worker.py's
daily flush.

THE BUG THIS FIXES
-------------------
utils/sent_template_store.py's has_sent()/mark_sent() are two separate
file reads/writes with real `await` points in services/campaign_service.py
sitting between them (property_service.resolve_property, wati_client.
send_template). Both campaign_worker's poll and queue_flush_worker's cron
run on the SAME asyncio event loop and can both call process_lead() /
process_generic_lead() for the SAME lead_id - e.g. a lead queued
off-hours whose CRM status was never updated (see campaign_service.py's
module docstring: the CRM update only happens AFTER a successful send,
so a still-queued lead's CRM record still looks "new") gets re-fetched
by the regular poller around the same time queue_flush_worker is
draining it from pending_queue.json. If both reach the has_sent() check
before either has called mark_sent(), both see False and both proceed
to actually call wati_client.send_template() - the customer gets the
template twice, sometimes with visibly different personalization
between the two sends (different property-name formatting, extra
whitespace in the name field) since each path resolved the lead fresh.

THE FIX
--------
A per-lead_id asyncio.Lock, held for the ENTIRE has_sent-check-through-
mark_sent critical section in campaign_service.py. Whichever caller
(regular poll or flush) gets there first runs the whole section
uninterrupted; the second caller blocks until the first is done, then
re-checks has_sent() (now True) and correctly skips instead of racing.

Deliberately in-memory, not persisted - same tradeoff already accepted
for campaign_worker's own _processed_lead_ids set. Both workers that
need this run in the SAME process/event loop (see app.py's lifespan),
so a lock that doesn't survive a restart is fine: a restart can only
ever race with itself after it comes back up, at which point this
module is freshly imported with an empty lock dict anyway - no
different from the process starting from nothing.

Locks are created lazily and left in the dict indefinitely (never
removed) - a real, bounded set of lead_ids per deployment lifetime at
this service's volume, not worth the added complexity of cleanup for
what is, in practice, a small dict of released Lock objects.
"""
import asyncio
from typing import Dict

_locks: Dict[str, asyncio.Lock] = {}


def _lock_for(lead_id: str) -> asyncio.Lock:
    lock = _locks.get(lead_id)
    if lock is None:
        lock = asyncio.Lock()
        _locks[lead_id] = lock
    return lock


class _LeadLockContext:
    """Thin async context manager wrapper so call sites can write
    `async with lead_send_lock.guard(lead.id):` directly, matching the
    style of Phase 1's conversation_lock.acquire()/release() pair but
    as a proper context manager since this is asyncio, not sync code."""

    def __init__(self, lead_id: str):
        self._lead_id = lead_id
        self._lock = None

    async def __aenter__(self):
        # A lead with no id can't be locked meaningfully - callers
        # should already guard against this (process_lead/process_
        # generic_lead both construct CampaignRecord from lead.id
        # unconditionally), but degrade to a no-op lock rather than
        # raising, consistent with this codebase's "never crash on
        # bad/missing data" convention elsewhere (see e.g.
        # pending_queue.enqueue's own lead.id guard).
        if not self._lead_id:
            return self
        self._lock = _lock_for(self._lead_id)
        await self._lock.acquire()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self._lock is not None:
            self._lock.release()
        return False


def guard(lead_id: str) -> _LeadLockContext:
    """Usage: `async with lead_send_lock.guard(lead.id): ...`
    Serializes every caller that races on the same lead_id through the
    wrapped section; different lead_ids never block each other."""
    return _LeadLockContext(lead_id)
