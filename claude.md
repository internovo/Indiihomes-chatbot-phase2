# Indihomes Phase 2 Campaign Service - working notes

This file tracks implementation decisions and design rationale for changes
made to this codebase, the same way the Phase 1 bot's own claude.md does
(`Indihomes-chatbot-V1/claude.md`) - read that file too if you're working
across both projects, since some decisions here directly mirror ones made
there and reference them rather than re-explaining from scratch.

---

# TASK: Business-hours gating (10 AM - 7 PM IST)

## Source

Implemented from `Indihomes_Business_Hours_Gating.docx` §3.1 and §3.3. §3.2
(the Phase 1 reactive bot's own gating) was implemented separately in
`Indihomes-chatbot-V1/claude.md` - read that file for the full doc context
and for the companion implementation in that codebase, since both share the
same `business_hours.py` file and the same underlying design doc.

## What was built

### 1. `business_hours.py` (new) - identical copy of Phase 1's

Same file as `Indihomes-chatbot-V1/business_hours.py`, copied in unchanged.
Deliberately zero dependencies beyond the stdlib, specifically so it can be
dropped into either codebase without pulling the other project's
`requirements.txt` along with it. Adds `IST_NAME = "Asia/Kolkata"` (a plain
string, not a `ZoneInfo` object) for APScheduler's `cron` trigger, which
needs a timezone name string - this constant didn't exist in Phase 1's copy
since that project never needed a cron-scheduled job in IST.

**If `business_hours.py` is ever edited in one project, mirror the change
in the other** - there's no shared package between these two codebases, so
nothing enforces they stay identical automatically. `tests/test_business_hours.py`
here re-runs the same boundary tests as Phase 1's own suite specifically so
a silent divergence gets caught by whichever project's tests run, not
neither.

### 2. `utils/pending_queue.py` (new)

The persisted off-hours queue from §3.3, mirroring `utils/sent_template_store.py`'s
exact file-I/O pattern (atomic tmp+replace JSON, same `state/` directory,
same Railway Volume - nothing new to provision).

**Design choice worth calling out:** stores just enough to reconstruct the
original `Lead` (phone, name, source, project code/name, timestamps) rather
than the already-resolved `Property`. The flush job re-runs
`campaign_service.process_lead` / `process_generic_lead` from scratch on
each flushed lead - the exact same functions the normal poll cycle uses -
so a stale overnight property snapshot is never sent, and every existing
safeguard (idempotency check, CRM-update isolation, per-lead exception
handling) applies to a flushed lead identically to a freshly polled one,
with zero duplicated logic.

`enqueue()` is idempotent by `lead_id`: re-queuing an already-queued lead
updates it in place rather than duplicating the entry, and preserves the
*original* `queued_at` so "oldest lead first" reflects when a lead first
went off-hours, not when it was last touched.

### 3. `services/campaign_service.py` - the gate itself

This is the ONE place the gate needed to go, and it's a nicer shape than
Phase 1's per-endpoint gating: `process_lead` and `process_generic_lead`
are the single funnel every send goes through, called from THREE places -
the normal poll cycle (`campaign_worker.py`), the retry cycle
(`retry_worker.py`), and now the flush job (`queue_flush_worker.py`).
Putting the gate inside these two functions means all three automatically
respect business hours with no duplicated logic and no risk of one call
site forgetting to check.

**Placement of the check matters and is deliberate**, matching the doc's
own ordering ("classify source → resolve property → check
is_business_hours()"): the gate sits AFTER property resolution succeeds
(so a lead with no resolvable property still goes through the existing
failure path, not the off-hours path) and AFTER the `sent_template_store.has_sent()`
idempotency check (so a lead already sent - e.g. right at the 6:59 PM edge
- is never re-queued). Three-way branch: already sent → off-hours → send
now.

A new `CampaignStatus.QUEUED_OFF_HOURS` status was added
(`utils/constants.py`) - deliberately NOT the same as `RETRYING`. This
distinction matters: `RETRYING` means a transient failure that
`retry_worker.py`'s in-memory backoff queue (5min/15min/1hr) will
automatically retry; `QUEUED_OFF_HOURS` means a successful resolution that
is simply waiting for a known future time. Confusing the two would either
have off-hours leads exhausting retry attempts and getting abandoned
(wrong - they didn't fail), or transient failures waiting until tomorrow's
flush instead of retrying within the hour (also wrong).

### 4. `workers/campaign_worker.py` - updated bookkeeping

`_summarize_and_queue()` now returns a 4th count, `queued_off_hours`, and
critically does **not** hand `QUEUED_OFF_HOURS` records to
`retry_worker.queue_for_retry()` - that status already persisted itself to
`pending_queue.json` inside `campaign_service.py`; queuing it a second time
in `retry_worker`'s in-memory queue would double-track the same lead under
two different flush schedules.

The 45-second poll and checkpoint advancement (`checkpoint.get_after_date()`
/ `checkpoint.save_checkpoint()`) are **completely untouched** - exactly as
the doc requires ("this must not change, or the afterDate checkpoint drifts
and leads get missed or re-fetched"). The gate only affects what happens
inside the two `process_*` functions the poll cycle already calls; nothing
about which leads get fetched or how far the checkpoint moves changed.

### 5. `workers/queue_flush_worker.py` (new)

Drains `pending_queue.json` oldest-first once daily, re-running
`process_lead` / `process_generic_lead` per entry (see point 3 above for
why no separate send path exists). Per-lead failure isolation: an
exception from a processor is already handled inside `process_lead` /
`process_generic_lead` themselves (they never raise), so one bad lead
can't stop the rest of the drain.

Defensive handling for a lead that comes back `QUEUED_OFF_HOURS` again
during a flush run (should be unreachable in normal operation - the flush
only runs once business hours have opened - but possible if the flush
schedule and `BUSINESS_START` ever drift out of sync after a config
change): logged loudly, and the entry is deliberately left in the queue
rather than removed, so a scheduling misconfiguration degrades to "flushes
a day late" rather than "silently loses the lead."

A failure during the flush itself (e.g. WATI briefly down at 10:00 AM)
gets the normal `retry_worker` backoff treatment, not another day in
`pending_queue` - a transient failure during flush is exactly the kind of
thing `retry_worker` already exists to handle well.

### 6. `app.py` + `config.py` - scheduling

New `cron` job (not `interval`, unlike the other three workers) since this
must fire at a specific wall-clock time in IST regardless of process start
time. The hour/minute are configurable via `settings.queue_flush_hour_ist`
/ `queue_flush_minute_ist` (default 10:00), following the same pattern as
every other worker interval in `config.py`, rather than hardcoded in the
worker module.

### 7. `routes/health.py`

Now reports `business_hours` ("open"/"closed"), `pending_off_hours_queue`
(count), and `oldest_queued_at` (timestamp of the longest-waiting queued
lead, or `null` if empty) - visibility into whether the gate is actually
being hit and how large the backlog is before the next flush.

## A genuine contradiction in the source document - resolved, not silently

`Indihomes_Business_Hours_Gating.docx` §3.3 disagrees with itself on the
flush time:
- The prose says: *"flushes the queue at 9:00 AM IST"*
- Its own code example sets `hour=9` but comments `# 10:00 AM IST daily`
  (the comment doesn't even match the code's own value)
- §4 "End-to-End Flow" says: *"10:00 AM IST → scheduled flush job drains
  pending_queue"*

**10:00 AM IST was chosen** (`config.py`'s default), for a reason beyond
just picking the majority: 9:00 AM is still fully within the same off-hours
window `business_hours.py` itself defines (`BUSINESS_START = 10:00`). A 9 AM
flush job would call `process_lead`/`process_generic_lead`, hit the exact
same `is_business_hours()` check this feature just added, read `False`, and
immediately re-queue everything it was trying to send - the flush job would
functionally do nothing at 9 AM. Flushing exactly at business open is the
only value that's internally consistent with the gate it's supposed to
drain. This is configurable (`queue_flush_hour_ist`) specifically so it's a
one-line change, not a code change, if this reasoning turns out to be wrong
or business hours themselves ever change.

## Scope boundary, called out explicitly (not silently decided)

`routes/campaign.py`'s `/property-detail` and `/notify-advisor` are
**reactive** webhook callbacks - they only fire after a campaign template
has already been sent and the lead has already engaged (tapped a button,
asked to book a site visit, etc.). The design doc's §3.3 scopes gating
specifically to *"the send step"* of the 45-second poll pipeline; nothing
in the doc asks for these reactive callbacks to be gated the way Phase 1's
equivalent endpoints were.

**These were deliberately left UNGATED.** A reasonable case exists for
gating them too (matching Phase 1's spirit of "no bot replies outside
hours" more completely, since a lead tapping "Talk to an Advisor" at 11 PM
would still get an instant `notify_advisor` email fired off-hours right
now) - but that's a scope expansion beyond what was asked for and beyond
what the doc describes, so it wasn't done. Flagging this explicitly rather
than either silently adding it or silently leaving a gap nobody knows
about: **if reactive campaign-flow callbacks should also respect business
hours, that's a follow-up task, not something assumed as part of this one.**

## Testing

```bash
cd Indiihomes-chatbot-phase2
pytest tests/test_business_hours.py -v
```

Covers: `business_hours.py` boundary times (mirrors Phase 1's own tests, so
a future divergence between the two copies is caught); `pending_queue.py`'s
full round trip (enqueue, load, idempotent re-enqueue preserving
`queued_at`, selective removal, oldest-first ordering); `process_lead` /
`process_generic_lead` queuing off-hours instead of sending (and NOT
touching the CRM); the ordering guarantee that an already-sent lead is
never re-queued even if somehow reprocessed off-hours; `campaign_worker`'s
`_summarize_and_queue` correctly counting `QUEUED_OFF_HOURS` without
handing it to `retry_worker`; and `queue_flush_worker`'s drain (normal
flush, the defensive still-off-hours case, oldest-first draining order,
and a no-op empty-queue run).

Run the full existing suite too, to confirm nothing here broke the
pre-existing checkpoint/retry/classification behavior the doc explicitly
requires stay unchanged:

```bash
pytest -v
```

Manual, once deployed:

```bash
curl https://<phase2-service>/health
# Look at "business_hours", "pending_off_hours_queue", "oldest_queued_at".
```

The most convincing manual check is time-dependent (a lead arriving
genuinely off-hours, confirmed queued via `/health`, then confirmed sent
after the next day's 10:00 AM flush) - the automated tests above use
`monkeypatch` on `is_business_hours()` directly rather than waiting for
real off-hours, for the same reason Phase 1's tests do.
