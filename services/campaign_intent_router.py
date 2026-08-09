"""
campaign_intent_router.py

A scoped free-text classifier for Phase 2's campaign flow (the
"View Details" / "What would you like to do next?" conversation), the
sibling of Phase 1's intent_router.py.

WHY THIS EXISTS
----------------
Confirmed live (7 Aug 2026): a Housing.com Property Campaign lead
(Monali, "Anchorpoint Aviara") replied to "What would you like to do
next?" with free text instead of tapping a button. The campaign flow's
button node had no free-text fallback at all - the exact same gap
Phase 1's Megha transcript exposed months earlier, just in a
codebase that never got the fix. See Indiihomes-chatbot-phase2's
claude.md, "Free-text handling in the campaign flow", for the full
incident writeup.

SCOPE - narrower than Phase 1's, and DELIBERATELY SO
------------------------------------------------------
Phase 1's intent_router.py has five intents because Phase 1's flow has
five corresponding actions available (search again, reject a
shortlist, restart, talk to an advisor, stop). Phase 2's campaign flow
has a much smaller action surface - one resolved project, no property
search, and (as of this writing) no visible slot-booking endpoint in
this codebase's routes/ - so this router only classifies what THIS
flow can actually act on:

    stop              "stop", "unsubscribe", "don't message me"
    talk_to_advisor   "talk to someone", "call me", "connect me"
    site_visit        "book a visit", "site visit", "want to visit"
    not_interested    "not interested", "no thanks"
    show_details      "tell me more", "details please", "send info again"

`site_visit` is intentionally routed to notify_advisor(), NOT to any
slot-listing/booking logic - there is no POST /available-slots or
/book-slot equivalent visible in this codebase's routes/. Handing it
to a human is the only safe default until the actual booking mechanism
for this flow is confirmed (it may be a WATI-native calendar
integration, or it may call Phase 1's booking endpoints directly - see
claude.md for the open question).

HONESTY NOTE ON PHRASING - this is the important caveat
----------------------------------------------------------
Unlike Phase 1's intent_router.py, which was built after seeing
Megha's ACTUAL words ("No one", "Send in borivali east also"), THIS
module was built WITHOUT ever seeing what Monali actually typed - her
transcript was never captured beyond "What would you like to do
next?". The phrase lists below are therefore inferred from the
CATEGORY of message that's plausible at this node (the same five
things Phase 1 needed), not grounded in real production text the way
Phase 1's were.

**Treat this as a v0, not a finished feature.** Once real logs exist
(see /interpret-message's logging in routes/campaign.py), come back
and true up these phrase lists against what people actually type here,
exactly the way Phase 1's intent_router.py was refined after Megha's
transcript. Shipping this now is still strictly better than the
current dead end - it just isn't calibrated to real data yet, and
that gap should be closed, not forgotten.
"""

from typing import Dict, List, Optional

_STOP_PHRASES = [
    "stop", "unsubscribe", "opt out", "opt-out", "don't message", "dont message",
    "don't contact", "dont contact", "remove me", "do not contact",
    "stop messaging", "stop texting",
]

_ADVISOR_PHRASES = [
    "advisor", "talk to someone", "talk to a person", "talk to a human",
    "speak to someone", "speak to a person", "speak to a human", "call me",
    "human please", "connect me", "agent", "sales person", "salesperson",
    "real person",
]

_SITE_VISIT_PHRASES = [
    "site visit", "book a visit", "book visit", "want to visit", "visit the",
    "see the property", "see the site", "schedule a visit", "want to see it",
    "can i visit", "book a site visit",
]

_NOT_INTERESTED_PHRASES = [
    "not interested", "no thanks", "no thank you", "not looking", "not right now",
    "not for me", "no longer interested", "don't want", "dont want",
]

_SHOW_DETAILS_PHRASES = [
    "tell me more", "more details", "more info", "send details", "send info",
    "details please", "more information", "know more", "send it again",
    "resend", "again please",
]


def _contains_any(text: str, phrases: List[str]) -> Optional[str]:
    for p in phrases:
        if p in text:
            return p
    return None


def classify(text: str) -> Dict:
    """Classify one piece of free text typed at the campaign flow's
    "what would you like to do next" node.

    Returns {"intent": "none"} or
    {"intent": "stop"|"talk_to_advisor"|"site_visit"|"not_interested"|"show_details",
     "matched_phrase": <the phrase that triggered it>}.

    Priority order, same reasoning as Phase 1's intent_router.classify:
      1. stop             - compliance-critical, must win over every other read
      2. talk_to_advisor   - an explicit request for a human should never be
                             reinterpreted as anything else
      3. site_visit         - a concrete next action, checked before the
                             softer "not_interested"/"show_details" reads
      4. not_interested
      5. show_details
    """
    raw = (text or "").strip().lower()
    if not raw:
        return {"intent": "none"}

    hit = _contains_any(raw, _STOP_PHRASES)
    if hit:
        return {"intent": "stop", "matched_phrase": hit}

    hit = _contains_any(raw, _ADVISOR_PHRASES)
    if hit:
        return {"intent": "talk_to_advisor", "matched_phrase": hit}

    hit = _contains_any(raw, _SITE_VISIT_PHRASES)
    if hit:
        return {"intent": "site_visit", "matched_phrase": hit}

    hit = _contains_any(raw, _NOT_INTERESTED_PHRASES)
    if hit:
        return {"intent": "not_interested", "matched_phrase": hit}

    hit = _contains_any(raw, _SHOW_DETAILS_PHRASES)
    if hit:
        return {"intent": "show_details", "matched_phrase": hit}

    return {"intent": "none"}
