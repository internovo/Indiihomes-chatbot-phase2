# understand.md — Phase 2 (the proactive Campaign Service)

> **Read this like a whiteboard session, not a reference manual.** It builds
> from the problem up. Read `../Indihomes-chatbot-V1/understand.md` first if
> you haven't — Phase 2 only makes sense once you understand that Phase 1 is
> *reactive* (the customer messages first) and Phase 2 is *proactive* (we
> message the customer first). `claude.md` in this folder is the decisions
> changelog; this file is the mental model behind it.

---

## 1. What problem are we actually solving?

Different scene from Phase 1. Here, the customer has **not** messaged us.

Priya filled out a form on Housing.com expressing interest in "Ariha
Opulence." Or she clicked a Meta (Facebook/Instagram) ad. That interest lands
in the Indihomes CRM as a **lead** — but she's never opened a WhatsApp chat
with us. She's a name, a phone number, and maybe a project she liked.

The **painful manual version**: someone has to constantly watch the CRM for
new leads, and for each one, look up the project details and manually send a
WhatsApp template ("Hi Priya, thanks for your interest in Ariha Opulence,
here are the details…") — fast, before the lead goes cold, 24/7.

Phase 2 automates *that*: it continuously watches the CRM, and the moment a
new lead appears, it sends the right opening WhatsApp template — which, once
Priya taps "Interested," hands her into Phase 1's qualification flow.

**So the two phases connect like this:**

```
Phase 2 (proactive)                    Phase 1 (reactive)
──────────────────                     ──────────────────
CRM gets a new lead                    Customer messages first
     │                                      ▲
     ▼                                      │
send opening template  ──► Priya taps ──────┘
"thanks for your interest"   "Interested"    then the qualification
                                             conversation runs
```

Phase 2's whole job is the arrow on the left: **CRM lead → first WhatsApp
touch.** After the tap, Phase 1 takes over.

---

## 2. The core mental model: a polling pipeline, not a webhook

Phase 1 is driven by *incoming* webhooks (WATI calls us). **Phase 2 is
driven by a clock.** Nobody calls Phase 2 to say "there's a new lead" — it
has to go *look*, over and over.

The honest one-liner: **Phase 2 = a loop that every 45 seconds asks the CRM
"anything new?" and messages whatever it finds.**

```
every 45 seconds (APScheduler)
        │
        ▼
"CRM, any leads newer than the last one I saw?"   ← GET /get-new-leads?afterDate=...
        │
        ▼
for each new lead:
    classify it → resolve its property → send the WhatsApp template → mark CRM
```

That "newer than the last one I saw" is the crucial bit. It's called a
**checkpoint**, and getting it right is most of what makes this service
correct. More in §4.

---

## 3. Where APScheduler is, and why (it runs FOUR jobs here)

Phase 1 used APScheduler for one gentle nudge. Phase 2 is *built on top of*
APScheduler — it's the engine. Four scheduled jobs, set up in `app.py`'s
`lifespan()`:

```
AsyncIOScheduler (started when FastAPI boots)
   │
   ├── campaign_worker      every 45s   → the main poll: find & message new leads
   ├── retry_worker         every 120s  → retry leads that failed to send
   ├── cleanup_worker       every 3600s → drop abandoned entries from the retry queue
   └── queue_flush_worker   daily 10 AM → send leads that were queued overnight (NEW)
```

Two different **trigger types**, and the difference matters:

- **`interval`** (the first three) — "every N seconds," relative to whenever
  the process started. Fine for "keep checking."
- **`cron`** (the flush job) — "at a specific wall-clock time," 10:00 AM IST,
  *regardless* of when the process started. You need this for the flush
  because "drain the overnight queue" has to happen at business open, not
  "45 seconds after the last restart."

```python
# interval — relative timing
scheduler.add_job(_run_campaign_cycle, "interval", seconds=45, ...)

# cron — absolute wall-clock timing, in IST
scheduler.add_job(_run_queue_flush_cycle, "cron", hour=10, minute=0,
                  timezone="Asia/Kolkata", ...)
```

Every job is wrapped so a crash in one cycle **never kills the scheduler** —
one bad poll must not stop all future polls:

```python
async def _run_campaign_cycle():
    try:
        await campaign_worker.run_cycle()
    except Exception:
        logger.exception("...")   # log and move on; next tick still fires
```

---

## 4. The checkpoint — the single most important idea in Phase 2

**The problem:** every 45 seconds we ask the CRM for leads. If we ask "give
me ALL leads" every time, we'd re-message everyone, forever. We need "give me
only leads I haven't seen yet."

**The solution:** a checkpoint — a saved timestamp of the newest lead we've
successfully processed. Next poll, we ask for leads *after* that time.

```
checkpoint.json:  { "last_processed": "2026-08-06T14:00:00.000Z" }
        │
        ▼
GET /get-new-leads?afterDate=2026-08-06T14:00:00.000Z
        │
        ▼
process the returned leads, then advance the checkpoint to the newest one
```

`utils/checkpoint.py` owns this file and nothing else touches it. The rule
that keeps it safe:

```python
def save_checkpoint(timestamp):
    # ONLY called after a successful cycle — a failed cycle must NOT
    # move the checkpoint forward, or failed leads get silently skipped.
```

**This is subtle and worth sitting with:** if the checkpoint advanced even
when processing failed, a lead that errored would be "in the past" next poll
and never retried. So the checkpoint only moves on success, and failures are
handled by a *separate* retry queue that doesn't depend on the checkpoint at
all.

### The war story that shaped this (the future-timestamp freeze)

**The trap we actually hit:** one real lead had a corrupted timestamp —
nearly 4 hours in the *future*. The checkpoint advanced to that future time.
Now every subsequent poll asked "any leads after [a time that hasn't
happened yet]?" — and got nothing, because nothing can be after a future
date yet. **The entire pipeline silently froze for hours.** Real leads in
that window were never even fetched.

**Two-part fix, both in `campaign_worker.py`:**

1. Ignore any timestamp more than 10 minutes in the future *for checkpoint
   purposes* — a corrupted future date can't drag the checkpoint past the
   present anymore:
   ```python
   if parsed - now > _MAX_FUTURE_SKEW:   # 10 minutes
       continue                          # don't let it set the checkpoint
   ```
2. Track `_processed_lead_ids` in memory — even if a lead keeps satisfying
   "afterDate" every cycle, we refuse to process (and re-message) the same ID
   twice in one session. You can see this firing in the production logs:
   *"Skipping 4 lead(s) already processed this session."*

The lead is still *processed* normally the first time — the future timestamp
only loses its power to move the checkpoint. That distinction (process the
lead, but don't trust its timestamp) is the whole fix.

---

## 5. How a lead gets classified and messaged

Once a poll fetches new leads, each one is sorted into one of three buckets
by **data presence, not source string** (`services/lead_service.py`):

```
                a new lead
                    │
        ┌───────────┼────────────────┐
        ▼           ▼                ▼
  Property      Generic          Ignored
  Campaign      Interest         (WhatsApp Bot / DIRECT —
  (has project  (name + phone     already handled by Phase 1,
   code/name)    only)            don't re-contact)
        │           │
        ▼           ▼
  look up the   just send a
  project, send  "thanks for your
  a property-    interest" opener
  specific
  template
```

- **Property Campaign** → `process_lead()`: resolve the project via the
  Indihomes API, build a template with its details, send it.
- **Generic Interest** → `process_generic_lead()`: no project to look up,
  just send the generic opener. Once tapped, WATI's own flow hands off to
  Phase 1.
- **Ignored** → leads that came *from* Phase 1 already (a customer who
  messaged us first); re-messaging them would be an annoying double-contact.

**Why "data presence, not source string"?** A Housing.com lead *might* arrive
with no project data. Classifying by the source label ("Housing.com →
property campaign") would then try to look up a project that isn't there.
Classifying by "does it actually have project data?" is robust to messy CRM
records.

---

## 6. The property lookup (how Phase 2 lists a property)

Different from Phase 1's search. Phase 1 *filters many* properties by
criteria; Phase 2 *resolves one specific* project a lead already named.

`services/property_service.py` does a **three-tier lookup**, each tier a
fallback for when the previous one can't find it:

```
resolve_raw_project(code, name)
        │
   1. fetch by project code   ── found? ─► done
        │ no
   2. fetch by exact name     ── found? ─► done
        │ no
   3. fuzzy search on first word, then verify full name
```

Tier 3 exists because of a real data-entry bug: a live project was stored as
`"Ariha  Opulence "` (double space, trailing space). An exact-name match
("Ariha Opulence") returned nothing. But you can't just take the first fuzzy
result either — there's *also* an "Ariha Vincere." So tier 3 searches broad
(first word "Ariha") then verifies each candidate against a
whitespace-normalized full-name comparison — broad enough to find the typo,
strict enough to reject the wrong project.

```python
def _normalize_for_match(text):
    return re.sub(r"\s+", " ", text).strip().lower()   # "Ariha  Opulence " == "ariha opulence"
```

`raw_to_property()` is the single place messy API JSON becomes a clean
`Property` — same idea as Phase 1's `_normalize()`, same reason (one place to
handle every quirk).

---

## 7. The idempotency problem — never send twice (the biggest war story)

This is the scariest class of bug in Phase 2, because getting it wrong means
**spamming a real customer's WhatsApp.**

**The trap we hit:** the CRM status update (telling the CRM "I've contacted
this lead") was failing with a 404 — *after* the WhatsApp template had
already gone out. The old code treated the whole operation as failed and
retried it. But a retry re-runs the *entire* function — including re-sending
the WhatsApp message. Result: the same customer got messaged again, and
again, every cycle.

**The fix — two layers:**

**Layer 1: isolate the CRM update from the send.** Once the message is out,
nothing after it can trigger a retry:

```python
# send the template — if THIS fails, retry is fine (nothing went out)
await wati_client.send_template(...)
record.mark_sent()

# update the CRM — its OWN try/except. If this fails, log loudly but
# DO NOT mark the whole thing failed. The message already went out;
# a retry would re-send it.
await _update_crm_status_after_send(...)   # swallows its own errors
```

**Layer 2: a durable "already sent" ledger** (`utils/sent_template_store.py`)
persisted to disk. Even if the process restarts and loses its in-memory
state, this file remembers we already messaged this lead+template:

```python
if sent_template_store.has_sent(lead.id, template_name):
    record.mark_sent()          # already done — do NOT send again
else:
    await wati_client.send_template(...)
    sent_template_store.mark_sent(lead.id, template_name)
```

**The lesson that generalizes:** any time an operation has an irreversible
side effect (sending a real message) followed by bookkeeping (updating a
status), the bookkeeping failing must **never** be able to re-trigger the
side effect. Separate them, and keep a durable record of the irreversible
part.

---

## 8. The two queues — and why they're different (don't confuse them)

Phase 2 has **two** separate queues, and understanding why they're separate
is understanding the service.

### Queue 1 — the retry queue (`workers/retry_worker.py`)

For **transient failures**: WATI timed out, the network blipped. These should
be retried *soon*, with backoff (5 min → 15 min → 1 hr). It's **in-memory** —
a restart clears it, which is fine, because a fresh checkpoint re-fetches
anything genuinely unsent.

```
send failed (network) → retry queue → try again in 5 min → 15 min → 1 hr
                                                   │ still failing after 3
                                                   ▼
                                        email an advisor, give up
                                        (never silently vanish)
```

### Queue 2 — the off-hours queue (`utils/pending_queue.py`) — the NEW one

For leads that **didn't fail at all** — they resolved perfectly, but arrived
outside 10 AM–7 PM IST. These aren't retried soon; they wait for a *known
future time* (tomorrow's 10 AM). So this queue is **persisted to disk** (a
restart at 2 AM must not lose an overnight lead) and drained by the **daily
cron flush**, not by backoff.

```
lead resolved but it's 11 PM → off-hours queue (on disk)
                                      │
                                      ▼ next day, 10:00 AM (cron)
                              queue_flush_worker drains it, oldest first,
                              re-running the SAME process_lead function
```

**Why not just use the retry queue for both?** Because a lead sitting in the
retry queue for 11 hours would exhaust its 3 attempts and get *abandoned* —
it would think it failed. An off-hours lead didn't fail; it's waiting. Two
fundamentally different situations → two queues with two different triggers.
Confusing them would either abandon good leads or delay real retries until
tomorrow.

---

## 9. Business-hours gating (the newest feature) — the proactive half

Recall Phase 1's gate was *reactive*: "customer messaged at 2 AM → reply with
a notice." Phase 2's is *proactive*: "we're about to message a customer at
2 AM → **don't**, queue them for 10 AM instead."

**Where the gate goes — one perfect choke point.** `process_lead` and
`process_generic_lead` are the *only* two functions that send a template, and
they're called from all three places sends happen (poll, retry, flush). Put
the gate inside them once, and everything respects it automatically:

```python
if sent_template_store.has_sent(...):        # 1. already sent? skip
    record.mark_sent()
elif not is_business_hours():                # 2. off hours? queue it
    pending_queue.enqueue(lead, category=...)
    record.status = CampaignStatus.QUEUED_OFF_HOURS
    return record                            # ← CRM NOT updated: not contacted yet
else:                                        # 3. send now
    await wati_client.send_template(...)
```

**The ordering is deliberate:** check "already sent" *before* "off hours," so
a lead sent at 6:59 PM is never re-queued. And the off-hours branch returns
*before* the CRM update — because the CRM must not show a lead as "contacted"
when it's actually still sitting in a queue.

### The document contradiction we had to resolve

The design doc said flush at "9:00 AM" in its prose, but its own code comment
said "10:00 AM," and its flow diagram said "10:00 AM." We chose **10:00 AM**
— and not just by majority vote. **9:00 AM is still inside the off-hours
window** (business opens at 10). A 9 AM flush would call `process_lead`, hit
the very same `is_business_hours()` check, read `False`, and immediately
re-queue everything it tried to send — doing nothing. Flushing *at* business
open is the only time that's internally consistent with the gate it drains.
(It's configurable via `queue_flush_hour_ist` in case that's ever wrong.)

---

## 10. Who owns what — the final table

| Layer | File(s) | Job |
|---|---|---|
| Scheduling engine | `app.py` (APScheduler) | Runs all 4 jobs; the clock |
| The main loop | `workers/campaign_worker.py` | Poll CRM, classify, dispatch |
| Send logic | `services/campaign_service.py` | The 2 process_* funcs; the ONE send path + business-hours gate |
| Lead classification | `services/lead_service.py` | Property Campaign / Generic / Ignored |
| Property resolution | `services/property_service.py` | 3-tier project lookup |
| Checkpoint | `utils/checkpoint.py` | "Newest lead I've seen" |
| Idempotency ledger | `utils/sent_template_store.py` | Durable "already sent" record |
| Retry queue | `workers/retry_worker.py` | Transient failures, backoff |
| Off-hours queue | `utils/pending_queue.py` + `workers/queue_flush_worker.py` | Overnight leads, daily flush |
| Time gating | `business_hours.py` | The 10–7 IST window (same file as Phase 1) |
| Reactive callbacks | `routes/campaign.py` | Post-send webhooks (property-detail, notify-advisor) |
| Health | `routes/health.py` | Queue sizes, business-hours status |

---

## 11. One sentence, and the next move

**Phase 2 is a clock-driven pipeline that polls the CRM every 45 seconds for
new leads, resolves each one's property, and sends the opening WhatsApp
template — guarded by a checkpoint so it never re-messages, a durable ledger
so a restart never double-sends, a retry queue for transient failures, and a
persisted off-hours queue that holds overnight leads until a daily 10 AM
flush.**

**Next move if you want to go deeper:** open `services/campaign_service.py`
and read `process_lead()` top to bottom. Every hard lesson in Phase 2 —
idempotency, the send/CRM-update isolation, and the business-hours gate — all
live in that one function, in the exact order they must run. Understand it and
you understand the spine of Phase 2.

---

## 12. How the whole system fits together (both phases, one picture)

```
   HOUSING.COM / META ADS form                CUSTOMER opens WhatsApp
             │                                        │
             ▼                                        │
      Indihomes CRM  ◄──── new lead                   │
             │                                        │
   ┌─────────┴──────────┐                             │
   │  PHASE 2           │                             │
   │  (proactive)       │                             │
   │  poll every 45s    │                             │
   │  send opening      │                             │
   │  template          │                             │
   └─────────┬──────────┘                             │
             │  Priya taps "Interested"               │
             ▼                                        ▼
        ┌──────────────────────────────────────────────────┐
        │  PHASE 1 (reactive) — the qualification flow      │
        │  area → budget → BHK → search → book → save-lead  │
        │  WATI = mouth/ears · FastAPI = brain · SQLite = memory │
        └──────────────────────────────────────────────────┘
```

Both phases share the *same* `business_hours.py` (copied, not imported —
they're separate deployments). Phase 2 *feeds* Phase 1. Neither knows how to
do the other's job, and that clean separation is exactly why each one is
testable and swappable on its own.

---

## 13. The duplicate-send race — two workers, one lead, no lock

§7 already covered the idempotency ledger (`sent_template_store.py`)
that stops a *retried* send from re-messaging a customer. This is a
different, subtler version of the same danger class: **two workers
racing each other**, not one worker retrying itself.

### The symptom, reported directly from production

Leads were receiving the opening template **twice** — one send
correlating with business-hours open, one at an unpredictable
("dynamic") time. The two copies sometimes differed slightly (markdown
asterisks around a project name present in one and not the other,
extra whitespace in a name) — a strong tell that each send
independently *re-resolved* the lead, rather than one send somehow
being delivered twice by the network.

### Why two workers could reach the same lead at all

Recall §8: an off-hours lead is queued, not sent, and its CRM status is
deliberately left untouched until an ACTUAL send succeeds (so the CRM
never shows "contacted" for a lead that's still waiting). That means a
still-queued lead can still look "new" to the regular poller's CRM
filter. Put those two facts together:

```
11:58 PM — lead arrives off-hours, gets queued (pending_queue.json)
            CRM status: still "new" (untouched — see §8)
            checkpoint may or may not have advanced past it yet

10:00 AM — queue_flush_worker's cron fires: reads pending_queue,
            calls process_lead() for this exact lead_id

~same moment — campaign_worker's regular 45s poll ALSO re-fetches this
            lead (CRM still says "new"), calls process_lead() for the
            SAME lead_id
```

Both calls run on the **same asyncio event loop** (one Python process,
see `app.py`'s `lifespan`). `sent_template_store.has_sent()` and
`.mark_sent()` are two separate file operations with real `await`
points between them (`property_service.resolve_property`,
`wati_client.send_template`). If both callers reach `has_sent()`
before either has reached `mark_sent()`, **both see `False`** and both
proceed to actually call WATI. This is a textbook TOCTOU
(time-of-check-to-time-of-use) race — the kind of bug that only shows
up under real concurrency, never in a single manual test.

### The fix — a per-lead lock around the WHOLE critical section

`utils/lead_send_lock.py`: a per-`lead_id` `asyncio.Lock`, held for the
*entire* check-through-send-through-mark section, not just the
initial read:

```python
async with lead_send_lock.guard(lead.id):
    if sent_template_store.has_sent(lead.id, template_name):
        record.mark_sent()          # already done elsewhere — skip
    elif not is_business_hours():
        pending_queue.enqueue(lead, ...)
        return record
    else:
        await wati_client.send_template(...)      # the network call
        sent_template_store.mark_sent(lead.id, template_name)
        record.mark_sent()
```

Whichever caller (poll or flush) gets there first now runs the whole
section uninterrupted; the second caller *blocks* until the first
finishes, then re-checks `has_sent()` — now `True` — and correctly
skips instead of racing. In-memory, not persisted, same tradeoff
already accepted for `_processed_lead_ids` (§4): both workers that
need this share one process, so a lock that resets on restart is fine.

**The generalizable lesson:** an idempotency *check* protects against
retries of the same call. It does NOT automatically protect against
two *different* callers reaching that check concurrently — that needs
an actual lock around the check-and-act section, not just a
check-before-acting pattern. This is the exact same class of bug
`Indihomes-chatbot-V1/conversation_lock.py` already existed to prevent
on the Phase 1 side; Phase 2 simply hadn't needed the equivalent until
two scheduled workers could legitimately touch the same lead.

### A real, separate lesson from fixing this: files can vanish between writing and deploying

After this fix was written, Railway crashed on the very next deploy —
an import error for `utils.lead_send_lock`, the module this fix
depends on. The cause turned out to be almost comically literal: the
new file existed on disk right after being created, but by the time
things were committed and pushed, it simply wasn't there anymore —
and because `campaign_service.py`'s import line for it *was* already
committed, the broken state got pushed as "clean" (`git status` showed
nothing to commit, because there was nothing left to notice was
missing).

**The practical habit this justifies:** after any multi-file change,
confirm every new file is still present immediately before committing
— `git status` telling you "working tree clean" only means "matches
what git already knows about," not "everything that should exist,
does."

---

## 14. The Phase 3 hook — notifying a salesperson after a successful send

Mirroring Phase 1's own hook (see that repo's understand.md §12),
`process_lead()` fires one more call after a template genuinely goes
out: `notify_lead_routing_best_effort(lead, prop)`, a POST to
`indihomes-lead-routing-service` (Phase 3).

```python
# past this point the CUSTOMER'S message has definitely gone out —
# nothing below this can ever re-trigger a resend
await notify_lead_routing_best_effort(lead, prop)
await _update_crm_status_after_send(indihomes_client, lead)
```

**Deliberately fired from BOTH the fresh-send branch and the
duplicate-skip branch above it** — both reach this point with the
record already marked sent. That's not an accident: it means if the
customer's template went out on a previous run but the salesperson
notification silently failed *that* time (Phase 3 unreachable, a
transient error), a later duplicate-skip pass still gets a chance to
retry just the notification — without ever risking a second customer
message, since that branch never reaches `wati_client.send_template`
at all.

**UPDATE (2026-08-12) - this hook is now LIVE, but with a carve-out:**
Phase 3 was deployed to Azure and `WATI_DRY_RUN` flipped off in
production. Real salesperson notifications went out for Property
Campaign leads - but the very first ones surfaced a real data gap:
Meta Ads leads' notifications arrived with `Looking For: -` and
`Budget: -`, because this repo's `Lead` model (`models/lead.py`) never
captured `configuration`/`budget` from the CRM in the first place -
`_build_payload()` in `integrations/lead_routing_client.py` had nothing
to forward. A notification missing the two fields most relevant to
actually following up reads as broken software, not "no data yet" -
worse than sending nothing.

**So `notify_lead_routing_best_effort()` is deliberately DISABLED for
Meta Ads as of 2026-08-12**, via a new settings flag
(`lead_routing_meta_ads_enabled`, env var
`LEAD_ROUTING_META_ADS_ENABLED`, defaults `false`), gated right at the
call site in `process_lead()`:

```python
settings = get_settings()
if settings.lead_routing_meta_ads_enabled:
    await notify_lead_routing_best_effort(lead, prop)
else:
    logger.info("Phase 3 salesperson notification skipped for lead %s (...)", lead.id)
```

See `claude.md`, "Meta Ads salesperson notification disabled", for the
full incident writeup and the exact steps to re-enable once
configuration/budget are actually captured from the CRM. Note this is
a **Phase 2-only** limitation - `Indihomes-chatbot-V1`'s own copy of
this hook (fired from `/search` and `/save-lead`) is unaffected and
still live, since direct-website leads genuinely do have real
configuration/budget data collected conversationally before Phase 3 is
ever called.

Updated picture of §12's diagram, with Phase 3 added:

```
   HOUSING.COM / META ADS form                CUSTOMER opens WhatsApp
             │                                        │
             ▼                                        │
      Indihomes CRM  ◄──── new lead                   │
             │                                        │
   ┌─────────┴──────────┐                             │
   │  PHASE 2 (proactive)│                            │
   │  poll every 45s     │                            │
   │  send opening       │                            │
   │  template           │                            │
   └──┬───────────────┬──┘                             │
      │ Priya taps    │ notify_lead_routing_best_effort │
      │ "Interested"  ▼                                 ▼
      │        ┌─────────────────────────────────────────┐
      │        │  indihomes-lead-routing-service (Phase 3) │
      │        │  resolves salesperson in Cosmos,          │
      │        │  notifies THEM on WhatsApp                │
      │        └─────────────────────────────────────────┘
      ▼                                                 ▲
 ┌──────────────────────────────────────────────────┐   │
 │  PHASE 1 (reactive) — the qualification flow      │───┘ (also calls Phase 3
 │  area → budget → BHK → search → book → save-lead  │      from /save-lead)
 └──────────────────────────────────────────────────┘
```
