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

---

# TASK: Free-text handling in the campaign flow

## The incident

Confirmed live, 7 Aug 2026: a Property Campaign lead (Monali, referred for
"Anchorpoint Aviara") received the opening template, tapped through to
view details, and hit:

```
Bot: What would you like to do next?
Bot: Indihomes
```

- a Buttons node. She then typed free text instead of tapping a button.
The transcript captured cuts off exactly at this point - **what she
actually typed was never captured**, so this fix could not be built
against her real words the way Phase 1's `intent_router.py` was built
against Megha's actual transcript.

**The gap itself, independent of her specific words:** this campaign flow
had NO equivalent of Phase 1's `intent_router.py`/`/interpret-message` at
all. Any free text at this node - regardless of what it said - had
nowhere to go. This is the same class of gap Megha's Phase 1 transcript
exposed months earlier, just in a codebase that never got the
corresponding fix.

## What was built

### 1. `services/campaign_intent_router.py` (new)

A scoped sibling of Phase 1's `intent_router.py`. Deliberately NARROWER -
five intents instead of Phase 1's five, but a DIFFERENT five, because this
flow's action surface is smaller: one resolved project, no property
search, and (confirmed by reading `routes/campaign.py`) no visible
slot-listing/booking endpoint in this codebase:

| Intent | Routed to |
|---|---|
| `stop` | `utils/opted_out_store.py` - see below |
| `talk_to_advisor` | `notify_service.notify_advisor(reason="advisor_requested")` |
| `site_visit` | `notify_advisor(reason="site_visit_requested_freetext")` - human handoff, see note below |
| `not_interested` | `notify_advisor(reason="not_interested_freetext")` |
| `show_details` | re-runs `campaign_property_service.resolve_campaign_property` |

**`site_visit` is intentionally routed to a human, not any automated
booking logic.** `routes/campaign.py` only exposes `/property-detail` and
`/notify-advisor` - there is no `/available-slots` or `/book-slot`
equivalent visible anywhere in this codebase. It's genuinely unclear
whether this flow's site-visit booking happens via a WATI-native calendar
integration, by calling Phase 1's booking endpoints directly, or some
other mechanism not visible from the code alone. Guessing at that
mechanism and wiring to it would risk a broken or duplicate booking path;
handing off to a human is the only universally-safe default until that's
confirmed. **Open question, not resolved here** - worth asking directly:
how does this flow's "Book a Site Visit" button actually book anything
today?

### 2. THE HONESTY NOTE - read this before touching the phrase lists

Unlike Phase 1's `intent_router.py`, whose phrase lists were built by
reading Megha's ACTUAL words ("No one", "Send in borivali east also"),
`campaign_intent_router.py`'s phrase lists are **inferred from the
CATEGORY of message plausible at this node**, not grounded in Monali's
real text - which was never captured. This is explicitly documented in
the module's own docstring as a v0, not a finished feature.

`POST /interpret-message` (below) logs every classification outcome,
including `"none"`, at INFO specifically so this can be trued up once real
data exists - the same iteration path Phase 1's router went through after
Megha's transcript. **Do not treat this phrase list as calibrated until
that happens.**

### 3. `utils/opted_out_store.py` (new) + wired into `services/campaign_service.py`

A Phase-2-specific do-not-contact list, separate from Phase 1's
`appointments_db.opted_out` table (different codebase, no shared
database). Same file-I/O pattern as `sent_template_store.py` /
`pending_queue.py`.

**Why Phase 2 needs its OWN copy of this concern, unlike most things
(which only need to exist in one phase or the other):** Phase 2
INITIATES contact. A phone that says "stop" here must not receive a
FUTURE campaign send when some OTHER lead record for the same phone shows
up later (a brand new Housing.com/Meta Ads form fill weeks after this
one) - a scenario that only applies to a service that proactively
messages people. Phase 1 never has this problem since it never initiates
contact.

Checked at the VERY TOP of `process_lead()` / `process_generic_lead()` -
before project resolution, before the `has_sent()` idempotency check,
before the business-hours gate. A new `CampaignStatus.OPTED_OUT` status
marks this terminal outcome distinctly from `RETRYING`/`QUEUED_OFF_HOURS`
- an opted-out lead is neither failing nor waiting, it's permanently done.

### 4. `routes/campaign.py` - `POST /interpret-message` (new)

Sibling of Phase 1's endpoint of the same name and same general shape
(flat string fields, `intent`/`is_global`/`handled`/`reply_text`). Calls
`campaign_intent_router.classify()`, dispatches to the five branches
above, logs every outcome.

**WATI wiring required - NOT done as part of this change.** Unlike Phase
1's `/interpret-message`, which was wired directly into a provided flow
JSON export, no campaign flow export was available to patch
programmatically here. The wiring pattern to follow is documented in
`Indihomes-chatbot-V1/claude.md`, "Required WATI wiring": find the "What
would you like to do next?" Buttons node, set its default/no-match path
(`interactiveButtonsDefaultNodeResultId`) to a new webhook node calling
this endpoint, then branch on `is_global` the same way Phase 1's
`main_condition-intglobal` does. **Also remember the `@`/`{{ }}` variable-
syntax trap documented in that same file (Bug 1 of the post-deploy
changelog)** when wiring the condition node - match the syntax to
whichever `responseVariables` type the webhook's fields are mapped as, not
by assumption.

### 5. `services/notify_service.py` - two new reason labels

`site_visit_requested_freetext` and `not_interested_freetext`, so an
advisor's inbox clearly distinguishes a free-text-triggered notification
from the existing button-triggered ones (`site_visit_no_slots`,
`advisor_requested`).

## Testing

```bash
cd Indiihomes-chatbot-phase2
pytest tests/test_campaign_intent_router.py -v
```

Covers: `campaign_intent_router.classify()` for all five intents plus the
empty/unrecognised cases; `opted_out_store.py`'s persistence round trip;
the opt-out gate correctly skipping BOTH `process_lead` and
`process_generic_lead` before any send, and NOT interfering with a normal
(non-opted-out) send; and `POST /interpret-message` end to end for `stop`
(confirms `opted_out_store` actually gets marked), `talk_to_advisor`,
`site_visit` (confirms it notifies rather than attempting a booking),
`not_interested`, `none`, and a minimal/empty body never erroring.

Called directly as async functions (`await interpret_message(...)`),
matching every other test file in this project's own convention - not
`TestClient(app)`, which would also spin up `app.py`'s real
`AsyncIOScheduler` lifespan and its four background jobs unnecessarily for
a unit test.

## Known limitations / open questions

- **Phrase lists are unvalidated against real data** - see the honesty
  note above. This is the most important limitation to remember.
- **The site-visit booking mechanism for this flow is unknown** to this
  implementation - `site_visit` hands off to a human specifically because
  of that uncertainty. Resolving the open question (how does "Book a Site
  Visit" actually book something today?) may make a more automated
  `site_visit` handler possible later.
- **WATI wiring is not done** - this endpoint exists and is tested, but
  nothing in WATI Builder points to it yet. No flow export was available
  to patch programmatically this time.
- **`show_details` re-resolves via `campaign_context`**, which depends on
  a `project_code` already having been remembered for this phone (set
  when the original template was sent). If that lookup fails for any
  reason, it notifies an advisor with `reason="unresolved_project"` rather
  than leaving the customer with no response at all.

---

# CHANGELOG: Lead-safety-net - `"none"` intent now notifies an advisor

## What was found

While auditing Phase 1's equivalent fallback path (see
`Indihomes-chatbot-V1/claude.md`, "Lead-safety-net"), the same gap turned
up here: `routes/campaign.py`'s `POST /interpret-message`, `kind == "none"`
branch returned `InterpretMessageResponse(intent="none", is_global="no",
handled="no")` and did nothing else. WATI's `is_global=="no"` condition
already falls through to whatever local retry copy that flow node has, so
the customer was never shown a blank message - but nobody was ever told a
lead hit a dead end here either.

**Where this diverges from Phase 1's fix**: Phase 1 has a local SQLite
database (`appointments_db.py`) to log an unresolved lead into. This
codebase has no local DB of its own for lead state - `notify_service.py`'s
advisor email already IS the log for every other unhandled case in this
file (`unresolved_project`, `lead_abandoned`, the two `*_freetext` reasons
below it). So the fix here is an advisor email, not a DB row.

## What was built

### 1. `models/notify.py` - `raw_message` field (new)

Optional field on `NotifyAdvisorRequest`, carrying what the lead actually
typed. Only populated for the new `unclassified_freetext` reason below -
every other existing reason already has enough context (project, slot,
advisor) without needing the raw text.

### 2. `services/notify_service.py` - `unclassified_freetext` reason (new)

Added to `_REASON_LABELS`, and `_body()` now includes a `Lead typed: ...`
line whenever `req.raw_message` is set (every other reason leaves it
unset, so this is additive and doesn't touch existing email bodies).

### 3. `routes/campaign.py` - `kind == "none"` branch (patched)

Now calls `notify_service.notify_advisor(...)` with
`reason="unclassified_freetext"` and the raw text, best-effort (the
function already swallows its own errors and returns a bool - same
contract as every other call site in this file), before returning the
same `InterpretMessageResponse` as before. No change to the customer-
facing behaviour; the only difference is an advisor now gets an email.

**Deliberately unthrottled**, matching how every other reason in this file
already behaves (one email per event, no rate limiting) - consistent with
the existing pattern rather than a special case for this one reason. If
real traffic shows this getting noisy (e.g. a lead repeatedly sending
gibberish), the fix is to add throttling to `notify_service.notify_advisor`
itself so every reason benefits, not just this one.

## Testing

No new automated test added - `tests/test_campaign_intent_router.py`
already has a `"none"` case (see that file's existing coverage); extending
it to assert `notify_advisor` gets called is the natural next step but
wasn't done as part of this pass. Manual check:

```bash
curl -X POST http://localhost:8000/interpret-message \
  -H "Content-Type: application/json" \
  -d '{"phone": "919999900199", "message": "asdkjfh gibberish"}'
# Expect: 200, {"intent": "none", "is_global": "no", "handled": "no"} - same
# response shape as before. Check logs / Brevo dashboard (or the console
# log line if BREVO_API_KEY isn't set) for an advisor email with subject
# containing "typed something the bot couldn't classify".
```

## Known limitations

- **No local log of unclassified messages in this codebase** - the advisor
  email is the only record. If Brevo is misconfigured (`BREVO_API_KEY` not
  set), `email_client.send()` only logs the email content at INFO level
  rather than actually notifying anyone - worth confirming this is
  configured in production before relying on this as the safety net it's
  meant to be.
- **Still no WATI wiring for this endpoint at all** - the pre-existing
  limitation from the "Free-text handling in the campaign flow" section
  above still applies; this change only fixes what happens once a message
  DOES reach `/interpret-message`, not whether WATI is actually calling it
  yet.

---

# CHANGELOG (Phase 1 note, recorded here too since Phase 1's own claude.md
# already covers it in full): the fallback nodes were dead ends

Live-tested by the team (Yashh Rane) right after importing the
lead-safety-net flow: reaching `main_message-fb-fallback` at the priority
question left the conversation with nothing listening for the next
message. Fixed in `Indihomes-main_v3_lead-safety-net.json` (Phase 1) by
looping `main_message-fb-ack` / `main_message-fb-fallback` into a new
`main_question-fb-continue` → `main_webhook-fb-continue` →
`main_condition-fb-stop` cycle, with `stop` carved out to a genuine
terminal message instead of looping. See
`Indihomes-chatbot-V1/claude.md`, "Lead-safety-net", for the full detail -
noted here only because Phase 2's `campaign_intent_router` flow (once
wired) will need the identical fix once it has its own button-node
defaults, so don't repeat the same dead-end mistake there.

---

# Phase 3 — Lead routing to salesperson (new)

**What changed:** `process_lead()` in `services/campaign_service.py` now
calls the NEW `indihomes-lead-routing-service` (separate repo,
`C:\Users\admin\Desktop\indihomes-lead-routing-service`) right after the
customer's opening WATI template send succeeds - fired in BOTH the
fresh-send branch and the `sent_template_store`-duplicate-skip branch,
since the routing service is itself idempotent per `lead.id` and this
catches the case where the customer template went out on a prior cycle
but the salesperson notification silently failed that time.
EXPLICITLY NOT called from `process_generic_lead()` - Generic Interest
leads have no `project_code` to resolve a salesperson against.

**New file:** `integrations/lead_routing_client.py` - same shape as
`integrations/wati_client.py` / `integrations/indihomes_client.py`
(httpx.AsyncClient, `with_retry`, lazy singleton). Exposes
`notify_lead_routing_best_effort(lead, prop)`, which NEVER raises - same
non-negotiable rule as `_update_crm_status_after_send`: this fires AFTER
the WATI send to the CUSTOMER already succeeded, so nothing here may ever
cause that to be retried (a retry would re-send the customer's own
template, not just re-notify the salesperson).

**New config fields** (`config.py`): `lead_routing_url`,
`lead_routing_shared_secret`, `lead_routing_dry_run` (default `True`),
`lead_routing_timeout_seconds`. Blank `lead_routing_url` = the hook is a
silent no-op, same pattern as every other optional integration in this
file (e.g. blank `BREVO_API_KEY`).

**Idempotency key sent:** `meta_ads:{lead.id}` - matches the routing
service's own idempotency contract
(`indihomes-lead-routing-service/app/utils/idempotency.py`), so a repeat
call for the same lead never double-messages a salesperson even across
multiple Phase 2 cycles.

See `indihomes-lead-routing-service/README.md` for the full Phase 3
design, including the one open verification item (confirming the real
Cosmos `salesPerson`/`salesPersonNumber` field names before going live).

---

# CHANGELOG: fixed a real duplicate-send bug - leads getting the campaign template twice

## What was found

Reported directly from production: leads ("N" / Kolte Patil Verve,
"Teja Singh" / Sushanku Avenue 37, "Hitesh" / Ariha Opulence, and
"almost all" leads per the report) each received the opening WATI
template TWICE - one send correlating with business-hours open, one at
an unpredictable ("dynamic") time. The two copies sometimes differed
slightly in personalization (markdown asterisks around the project
name present in one copy and not the other, extra whitespace in a
name), consistent with each send having independently re-resolved the
lead rather than being a single send somehow delivered twice.

## Root cause

`utils/sent_template_store.py`'s `has_sent()` / `mark_sent()` are two
separate, non-atomic file operations, with real `await` points
(`property_service.resolve_property`, `wati_client.send_template`)
sitting between the check and the mark inside
`services/campaign_service.py`'s `process_lead()` / `process_generic_lead()`.

`workers/campaign_worker.py`'s regular 45-second poll and
`workers/queue_flush_worker.py`'s once-daily 10:00 AM IST cron both run
on the SAME asyncio event loop (see `app.py`'s `lifespan`) and can both
end up calling `process_lead()` for the SAME lead_id: a lead queued
off-hours has its CRM status left untouched until an ACTUAL send
succeeds (`_update_crm_status_after_send` only runs after a real send -
see that function's own docstring), so a still-queued lead can still
look "new" to the CRM's own `get-new-leads` filter and get re-fetched
by the regular poller around the same time `queue_flush_worker` is
draining it from `pending_queue.json`. If both reach the `has_sent()`
check before either has called `mark_sent()`, both see `False` and both
proceed to actually call `wati_client.send_template()` - a genuine
double send to a real customer, not a display/logging artifact.

This is the exact class of bug `Indihomes-chatbot-V1/conversation_lock.py`
already exists to prevent on the Phase 1 side (search, save-lead,
book-slot); Phase 2 never had the equivalent lock around its own send
path.

## What was built

**`utils/lead_send_lock.py`** (new) - a per-`lead_id` `asyncio.Lock`,
exposed as `async with lead_send_lock.guard(lead.id): ...`. In-memory,
not persisted (same tradeoff already accepted for `campaign_worker`'s
own `_processed_lead_ids` set) - both workers that need this share one
process/event loop, so a lock that resets on restart is fine.

**`services/campaign_service.py`** - the entire `has_sent()`-check-
through-`mark_sent()` section in both `process_lead()` and
`process_generic_lead()` is now wrapped in `async with lead_send_lock.
guard(lead.id):`. Whichever caller (regular poll or flush) gets there
first runs the whole section - including the awaited WATI send -
uninterrupted; the second caller blocks until the first finishes, then
re-checks `has_sent()` (now `True`) and correctly skips instead of
racing. The `QUEUED_OFF_HOURS` early `return record` inside the lock
block still releases the lock correctly (Python context managers run
`__aexit__` on any exit path, including an early `return`).

## Testing

`tests/test_campaign_worker.py` -
`test_process_lead_does_not_double_send_when_called_concurrently_for_the_same_lead`
(new): calls `process_lead()` twice concurrently via `asyncio.gather`
for the same lead, using a `SlowWatiClient` whose `send_template()`
deliberately yields control mid-call (`asyncio.sleep`) to force the
exact interleaving that made the race possible in production. Asserts
exactly one WATI send happens. Run:

```powershell
cd C:\Users\admin\Desktop\Indiihomes-chatbot-phase2
python -m pytest tests/test_campaign_worker.py -v
```

Manual sanity check before trusting this in production: deploy, then
watch `state/sent_templates.json` and `state/pending_queue.json` for a
few real leads that arrive close to business-hours open - a lead
should appear in `sent_templates.json` exactly once, and be removed
from `pending_queue.json` the same cycle it's marked sent, never both
queued AND independently sent again later the same day.

## Known limitations

- **This closes the race between workers in ONE process.** If this
  service is ever scaled to more than one running instance (multiple
  Railway replicas), an in-memory per-process lock can't coordinate
  across processes - `sent_template_store`'s file-based check would
  still be the only guard at that point, and this same race would
  reopen across instances. Not a concern at the current single-instance
  deployment, but worth remembering before ever scaling this service
  horizontally - a distributed lock (e.g. backed by the same Railway
  Volume, or Cosmos) would be needed at that point, not this fix.
- **Doesn't address why a queued lead's CRM status looks "new" to the
  regular poller in the first place** - the lock prevents the resulting
  double SEND, but the underlying re-fetch of an already-queued lead
  still happens on every poll cycle until it's actually sent. Harmless
  now (it just re-queues in place, per `pending_queue.enqueue()`'s own
  idempotent-by-lead_id behavior) but worth knowing if pending_queue
  ever grows large enough that redundant re-fetches become a real cost.

---

# INVESTIGATION: multi-property selection bug ('1 & 2') - does NOT apply here

## Context

Phase 1 shipped a fix for a real production bug (Smriti transcript): a
customer replying "1 & 2" to "Which one would you like to see in detail?"
only ever saw property #1, because `_parse_choice()` used `re.search(r"\d+",
choice)` - first match only, silently dropping every number after the
first. See `Indihomes-chatbot-V1/claude.md`, "multi-property selection",
for the full writeup. Asked to port the same fix here.

## Finding: this codebase has no equivalent bug to fix

Checked `models/property_detail.py`, `routes/campaign.py`, and
`services/campaign_property_service.py` directly before assuming a port
was needed. **There is no numbered-list flow anywhere in Phase 2.**

`PropertyDetailRequest` (the wire format for the campaign flow's
`/property-detail` webhook) has exactly two fields: `phone` and
`project_code`. There is no `choice` field, no shortlist, no
`_parse_choice`/`_parse_choices` equivalent anywhere in this codebase to
even contain the bug. Structurally, this is because Phase 2's campaign
flow works completely differently from Phase 1's: every Property Campaign
lead is ALREADY resolved to exactly ONE specific project before the
customer ever replies to anything - either via the `project_code` WATI
passes directly, or via `campaign_context`'s phone-to-project_code mapping
recorded at the moment the opening template was sent
(`campaign_property_service.resolve_campaign_property`). There is no
moment in this flow where a customer is shown several numbered properties
and asked to pick - the entire "reply with a number, we resolve it against
a saved shortlist" mechanism Phase 1's bug lived inside simply isn't part
of this architecture.

Also checked `services/campaign_intent_router.py`'s `classify()` for the
same CLASS of bug (a first-match-only parser silently dropping additional
valid input) in case it showed up somewhere unexpected. It doesn't: its
phrase-priority ordering (`stop` checked before `talk_to_advisor` before
`site_visit`...) is an INTENTIONAL single-intent-per-message design (`stop`
must always win, per its own docstring), not an accidental truncation of a
list the customer meant to fully specify. A message that genuinely mixes
two intents ("not interested in this one, but tell me more about others")
is a real, different, harder problem - multi-intent-in-one-message
classification - not the numbered-list-truncation bug this investigation
was checking for. Not fixed here, since it's out of scope for a straight
port of the Phase 1 fix, and no production evidence exists yet that it's
actually happening.

## Conclusion

**No code change made in this pass.** Forcing an artificial `_parse_choices`-
style function into a codebase with nothing for it to parse would be
inventing a fix for a problem that doesn't exist here, not porting a real
one. Documenting the investigation itself (not just the non-outcome) so
this doesn't get silently re-asked or re-investigated from scratch later -
if Phase 2's campaign flow ever grows a genuine multi-property listing
feature (e.g. a "here are 3 similar projects" recommendation flow), THIS
is the section to revisit, and the Phase 1 fix
(`Indihomes-chatbot-V1/app.py`'s `_parse_choices`) is the pattern to copy
at that point - not before.

---

# UPDATE: WATI wiring done + the site-visit booking open question resolved

## Open question from "Free-text handling in the campaign flow" - now answered

That section flagged: *"how does this flow's 'Book a Site Visit' button
actually book anything today?"* - `site_visit` was routed to a human
notification specifically because the mechanism was unknown.

**Answered while patching the real `phase2-v3(prod)` WATI flow JSON**:
`camp_webhook-slots` and `camp_webhook-book` in that flow point at
`https://web-production-ea977.up.railway.app/available-slots` and
`/book-slot` - **Phase 1's backend**, not this one. This campaign flow
reuses Phase 1's real Google Calendar / booking infrastructure directly;
there's no separate Phase 2 booking mechanism to build.

## WATI wiring completed for `phase2-v3(prod)`

The flow JSON was uploaded and patched directly (not hand-edited) -
`phase2-v3_prod__updated.json`. Both `InteractiveButtons` nodes
(`camp_buttons-confirm`, `camp_buttons-next`) now have their default paths
wired to `POST /interpret-message`, each with its own self-contained
5-condition chain (`stop`/`talk_to_advisor`/`site_visit`/`not_interested`/
`show_details`), all routing to REAL existing nodes wherever one exists -
including `site_visit` now correctly routing to the REAL
`camp_webhook-slots` node (confirmed working per the discovery above),
not a generic human-notification fallback the way
`campaign_intent_router.py`'s own backend handler still does.

**This means `campaign_intent_router.py`'s own `site_visit` handler
(routing to `notify_advisor(reason="site_visit_requested_freetext")`) is
now effectively BYPASSED for this specific WATI flow** - the flow's
condition chain intercepts `site_visit` and jumps straight into the real
booking flow before the backend's own reply_text would ever be shown. The
backend handler still exists and still fires correctly for OTHER callers
of `/interpret-message` (e.g. the Meta Ads flow, or any future flow that
doesn't have its own real booking flow to jump into) - this is a case
where the WATI-side wiring can be SMARTER than the generic backend
fallback when it has more context available (a real, working node to
route to) than the backend does. See `Indihomes-chatbot-V1/claude.md`'s
"not these reject_all gap + two WATI flows patched" changelog for the full
design writeup of this patch (built there since it covers both flows in
one pass) - full mechanical detail not repeated here.

## Known limitations updated

- The "WATI wiring is not done" limitation from "Free-text handling in the
  campaign flow" is **resolved** for `phase2-v3(prod)`. Still verify
  visually in Builder after import (per the caution in the Phase 1
  changelog referenced above) before trusting it live.
- The "site-visit booking mechanism for this flow is unknown" limitation
  is **resolved** - see above. `campaign_intent_router.py`'s own
  `site_visit` handler remains a human-notification fallback for OTHER
  callers where no real booking flow exists to jump into - this is correct
  and intentional, not a leftover to clean up.
- Phrase-list validation against real data (the honesty note) **still
  applies** - unchanged by this update.

---

## Meta Ads salesperson notification disabled (2026-08-12)

**What happened:** once `indihomes-lead-routing-service` (Phase 3) went
live in production (`WATI_DRY_RUN=false`), real salesperson notifications
started going out for Meta Ads leads with the "Looking For" and "Budget"
fields both showing `-` (the template's own fallback for an empty
value) - confirmed directly from real messages received on WhatsApp, e.g.:

```
Client Name: Nadeem sheikh
Mobile No: ****6879
Looking For: -
Budget: -
```

**Root cause:** `models/lead.py`'s `Lead` model has never had
`configuration` or `budget` fields, aliased or otherwise - it only
captures `id`, `name`, `phone`, `lead_source`, `project_code`,
`project_name`, `status`, and the two timestamp fields.
`integrations/lead_routing_client.py`'s `_build_payload()` accordingly
never included `configuration`/`budget` in the POST to Phase 3 at all -
not a bug in Phase 3, not a bug in the payload-sending code, just data
that was never captured from the CRM lead record in the first place.

**Why this needed an immediate fix, not just a backlog item:** a
salesperson notification missing the two fields most relevant to
actually following up (what the customer wants, what they can spend)
reads as broken software, not "we don't have that data yet." Worse than
sending nothing at all.

**The fix - disable, don't patch blind:** rather than guess at what the
raw CRM lead JSON's actual field names for configuration/budget are (see
`scripts/dump_get_new_leads_raw.py` for how to find out for real), Meta
Ads salesperson notifications are disabled outright via a new settings
flag, `lead_routing_meta_ads_enabled` (env var
`LEAD_ROUTING_META_ADS_ENABLED`, defaults `false`). Gated in
`services/campaign_service.py::process_lead()`, right where
`notify_lead_routing_best_effort()` used to be called unconditionally:

```python
settings = get_settings()
if settings.lead_routing_meta_ads_enabled:
    await notify_lead_routing_best_effort(lead, prop)
else:
    logger.info("Phase 3 salesperson notification skipped for lead %s (...)", lead.id)
```

**What this does NOT affect:**
- The customer-facing campaign template send (`wati_client.send_template`)
  - completely unrelated code path, still sends normally.
- Phase 1 (`Indihomes-chatbot-V1`)'s own Phase 3 hooks
  (`notify_recommendations()` at `/search`, `route_lead()` at
  `/save-lead`) - direct-website leads DO have real configuration/budget
  data collected conversationally, so those are unaffected and still live.
- `process_generic_lead()` - never called `notify_lead_routing_best_effort`
  in the first place (Generic Interest leads have no project to resolve a
  salesperson against), so nothing changed there.

**Re-enabling this later requires, in order:**
1. Run `scripts/dump_get_new_leads_raw.py` against a real Meta Ads lead to
   see the actual raw field names for configuration/budget (if they exist
   at all in the CRM record - they may simply not be collected by the ad
   form, in which case this may need a product conversation, not a code
   fix).
2. Add the confirmed fields to `models/lead.py` with the correct alias.
3. Update `integrations/lead_routing_client.py::_build_payload()` to
   forward them.
4. Set `LEAD_ROUTING_META_ADS_ENABLED=true` and verify with a real (or
   dry-run) send before trusting it in production again.

No test changes were needed - none of `tests/test_campaign_worker.py`'s
tests mock or assert on `notify_lead_routing_best_effort`, and this
change is strictly more conservative (skips a call that was previously
attempted unconditionally) than what was there before.

### UPDATE (2026-08-12, same day) - this is now the PERMANENT architecture, not a stopgap

Original framing above ("re-enable once configuration/budget are
captured") assumed the fix was: teach Phase 2 to collect that data and
call Phase 3 itself. Explicit product decision instead: **Phase 2 must
never call Phase 3 directly, for any lead, ever.** The only legitimate
trigger for a salesperson notification is `Indihomes-chatbot-V1`'s
`/search` endpoint - the moment an actual shortlist of properties is
shown to a lead, whoever originated them (organic Phase 1 conversation
OR a Meta Ads lead who tapped through into the qualification flow).

**Why this is the right trigger point, not just a workaround:** `/search`
is the one place in the whole system where real `configuration` and
`budget` are actually known - collected conversationally, question by
question, before the shortlist is ever shown. Nothing earlier in either
pipeline (Phase 2's CRM poll, this repo's own `Lead` model, the initial
template send) has that data or ever will, because it isn't collected
that early. Trying to teach Phase 2 to send Phase 3 notifications with
placeholder/incomplete data was always going to be worse than not
sending at all - the right fix isn't better data collection in Phase 2,
it's simply never triggering from Phase 2.

**Confirmed no other Phase 2 call site exists:** `routes/campaign.py`
(`/property-detail`, `/notify-advisor`, `/interpret-message`) never
references `notify_lead_routing_best_effort` or Phase 3 at all -
`process_lead()`'s now-disabled call (see above) was the only one in
this entire repo. `process_generic_lead()` never had one either. So the
2026-08-12 fix above is sufficient on its own - no further code change
needed for this decision; it was already structurally correct, just
documented as temporary when it's actually permanent.

**The one dependency this relies on that lives OUTSIDE this codebase:**
WATI's own flow-builder configuration must actually route a Meta Ads
lead's "Interested" tap into Phase 1's full qualification flow (area →
budget → configuration → `/search`), not just show one property's
detail and stop. If that routing isn't wired in WATI's dashboard, a
Meta Ads lead may never reach `/search` at all - meaning they'd never
get a salesperson notification, not just a delayed one. That's a WATI
flow-config question, not something visible in or fixable from this
repo's code - worth confirming directly in the WATI Flow Builder if
"Meta Ads leads never seem to trigger a salesperson notification even
after continuing the conversation" ever comes up as a report.

`config.py`'s `lead_routing_meta_ads_enabled` flag is kept as-is (still
defaults `false`) - not because re-enabling is expected, but because
removing it entirely would remove the one clean escape hatch if this
architectural decision is ever revisited. Its docstring there should be
read alongside this update, not instead of it.

---

# TASK: Lead events - feeding indihomes-os's Lead Capture UI

## What this is

The sibling of Indihomes-chatbot-V1's own "Lead events" task (see that
repo's `claude.md`) - indihomes-os's Lead Capture screen shows an "AI
Activity" tick (did the WhatsApp template actually reach this lead?) and
a vertical "Lead Journey" checkpoint tracker, for EVERY lead regardless of
which pipeline captured them. This repo's job is to feed real WhatsApp
delivery-status events for leads run through the Phase 2 campaign
pipeline (Property Campaign / Generic Interest).

## What was built

### `integrations/os_events_client.py` (new)

Same async httpx / `with_retry` / lazy-singleton shape as
`integrations/lead_routing_client.py` in this same folder - read that file
first if anything here looks unfamiliar. One function,
`emit_best_effort(phone, checkpoint, payload=None, source_ref="",
idempotency_key="")`, posting to indihomes-os's `POST /api/lead-events`.
Dry-run defaults `True` (`OS_EVENTS_DRY_RUN`, same convention as
`LEAD_ROUTING_DRY_RUN`) and blank `OS_EVENTS_URL` is a silent no-op - same
safety posture as the Phase 3 lead-routing hook.

### Four checkpoints wired at their natural, ALREADY-EXISTING decision points

This repo already tracks WhatsApp delivery status in real detail
(`utils/meta_delivery_store.py` - PENDING/DELIVERED/FAILED/RESENT). The
lead-events hook does NOT duplicate that tracking; it just emits
alongside it, at the exact point each state is already decided:

| Checkpoint | Where | Notes |
|---|---|---|
| `template_sent` | `services/campaign_service.py`, both `process_lead()` and `process_generic_lead()`, right after `meta_delivery_store.record_sent()` | Fires the moment a real send to the customer succeeds. |
| `delivered` | `routes/webhook.py`, on `sentMessageDELIVERED`/`sentMessageREAD` | See "a real trap caught" below for why this needed a new lookup helper. |
| `failed` | `routes/webhook.py`, on `templateMessageFailed` | Same lookup helper; payload carries `failed_code`/`failed_detail`. |
| `resent` | `workers/meta_resend_worker.py`, only on a successful 10 AM IST resend | Not fired on a resend attempt that itself failed - the `failed` checkpoint from the ORIGINAL send already told indihomes-os what it needs to know. |

### A real trap caught while wiring `delivered`/`failed`: the webhook payloads have no phone

`routes/webhook.py`'s own module docstring already documents this (a
real production finding from 16 Aug 2026): `sentMessageDELIVERED` and
`templateMessageFailed` webhooks carry **no phone number at all**, only
`whatsappMessageId`. `mark_delivered_by_message_id()` /
`mark_failed_by_message_id()` already knew how to resolve the record
internally, but returned only a `bool` - not enough for the lead-events
hook, which needs the actual phone to emit to indihomes-os.

**The fix**: a new, purely additive `get_record_by_message_id()` in
`utils/meta_delivery_store.py`, called right after the existing
`mark_*_by_message_id()` calls (which still run exactly as before, still
return `bool`, untouched) - the new function just re-looks-up the same
record to read its `phone` field back out. No existing function's
signature or contract changed.

### A second real trap, caught during THIS wiring, in the OTHER repo

indihomes-os's `lead-journey.cjs` defines the exact checkpoint vocabulary
`lead-events.cjs` will accept - and it did not originally include
`delivered`, `failed`, or `resent` at all (only the checkpoints
Indihomes-chatbot-V1 emits). Emitting them as written would have been
silently REJECTED by indihomes-os's own validation the moment that
backend is restored and wired up. Caught before it could ship silently -
see `indihomes-os-restructured/backend/LEAD_EVENTS_INTEGRATION.md`'s own
"Update" section for the fix (all three checkpoints added to the ladder,
plus a related dead-code bug fixed in `db.cjs`'s `getAiActivity()` at the
same time), and see that same file for the end-to-end re-verification
that was run afterward.

## Required env vars (see `.env.example`, already updated)

```
OS_EVENTS_URL=
OS_EVENTS_SHARED_SECRET=
OS_EVENTS_DRY_RUN=true
OS_EVENTS_TIMEOUT_SECONDS=10
```

## Known limitations

- **Not tested against a live indihomes-os** - same reason as
  Indihomes-chatbot-V1's own equivalent limitation: that backend doesn't
  exist yet. `os_events_client.py` itself follows the exact same,
  already-proven pattern as `lead_routing_client.py` in this file, which
  IS exercised in this repo's own test suite for the analogous Phase 3
  hook - the new client was reviewed against that pattern rather than
  independently sandbox-tested end-to-end (unlike Indihomes-chatbot-V1's
  copy, which was, since that repo had a convenient throwaway HTTP server
  test already built for the Phase 3 hook it mirrors).
- **`lead_replied`/`no_reply`/`followup_sent`-equivalent checkpoints are
  NOT emitted from this repo** - Phase 2 is a one-shot campaign sender,
  not a conversation tracker; once a lead taps into Phase 1's
  qualification flow (see this repo's own architecture diagrams), THAT
  repo's `followup_scheduler.py` already owns the equivalent signal. No
  duplicate/competing tracking was added here.
