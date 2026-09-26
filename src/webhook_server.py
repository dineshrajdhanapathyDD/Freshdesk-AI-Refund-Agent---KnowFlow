"""
Receives the webhook fired by a Freshdesk "Ticket Updates" automation rule,
then uses the Freshdesk Product MCP server to read the ticket, decide, and
act. See README.md for the automation rule setup.

Run: uvicorn webhook_server:app --host 0.0.0.0 --port 8000
"""

import asyncio
import logging
import os
import re
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, Request

from mcp_client import (
    call_tool,
    fetch_conversations,
    fetch_refund_policies,
    find_human_approval,
    freshdesk_mcp_session,
)
from policy import assess_refund, evaluate_refund
from ai import analyze_refund, write_approval_reply

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("refund-agent")

app = FastAPI()

FINANCE_GROUP_ID = int(os.environ.get("FRESHDESK_FINANCE_GROUP_ID", "0"))
RESOLVED_STATUS = 4  # Freshdesk default: 2 Open, 3 Pending, 4 Resolved, 5 Closed
PENDING_STATUS = 3   # Pending — used while awaiting human approval

# Matches money amounts in ticket text: "$50", "Rs. 1200", "1,500.00", "INR 300".
_AMOUNT_RE = re.compile(
    r"(?:rs\.?|inr|usd|\$|₹)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)"
    r"|([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:rs\.?|inr|usd|rupees|dollars)",
    re.IGNORECASE,
)


def _parse_amount(text: str) -> float:
    """Best-effort pull of a refund amount from free-text. 0 if none found."""
    if not text:
        return 0.0
    match = _AMOUNT_RE.search(text)
    if not match:
        return 0.0
    raw = match.group(1) or match.group(2)
    try:
        return float(raw.replace(",", ""))
    except (ValueError, AttributeError):
        return 0.0


def _order_age_days(ticket: dict) -> int:
    """
    ddretail's ticket form has no order-age field, so approximate order age
    with how long ago the ticket was created. Swap in a real order-date
    custom field here if/when the form captures one.
    """
    created = ticket.get("created_at")
    if not created:
        return 9999
    try:
        created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return (datetime.now(timezone.utc) - created_dt).days
    except (ValueError, AttributeError):
        return 9999


def extract_refund_request(ticket: dict) -> tuple[float, int]:
    """
    Pull the requested refund amount and order age (in days) off the ticket.

    ddretail's real tickets have no structured cf_refund_amount field — the
    amount lives in the email body (e.g. "please refund Rs. 1200"). So we
    parse the subject + description text. If a structured custom field is
    added later, prefer it over the text parse.
    """
    custom = ticket.get("custom_fields") or {}
    amount = float(custom.get("cf_refund_amount") or 0)
    if amount <= 0:
        text = f"{ticket.get('subject', '')} {ticket.get('description_text') or ticket.get('description', '')}"
        amount = _parse_amount(text)

    order_age_days = int(custom.get("cf_order_age_days") or 0) or _order_age_days(ticket)
    return amount, order_age_days


# --- Duplicate-charge signal extraction ------------------------------------

_DUP_HINTS = ("charged twice", "double charge", "duplicate", "charged me two", "two times", "twice for")
_ORDER_RE = re.compile(r"\b(?:order|ord)\s*(?:id|#|no\.?|number)?\s*[:#]?\s*([A-Z]{1,4}-?\d{3,})", re.IGNORECASE)
_TXN_RE = re.compile(r"\b(?:transaction|txn|payment)\s*(?:id|#|no\.?)?\s*[:#]?\s*([A-Za-z0-9-]{4,})", re.IGNORECASE)


def _ticket_text(ticket: dict) -> str:
    return f"{ticket.get('subject', '')}\n{ticket.get('description_text') or ticket.get('description', '')}"


def _detect_currency(text: str) -> str:
    if "$" in text or re.search(r"\busd\b|\bdollars?\b", text, re.IGNORECASE):
        return "USD"
    if "₹" in text or re.search(r"\brs\.?\b|\binr\b|\brupees?\b", text, re.IGNORECASE):
        return "INR"
    return "USD"


def build_refund_signals(ticket: dict) -> dict:
    """
    Turn a raw ticket into the normalized signals assess_refund() consumes.

    ddretail tickets carry no structured payment fields, so we extract what we
    can from the email text (order id, transaction id, amount, duplicate hint)
    and record which verification fields we could NOT confirm — those drive the
    Payment Verification policy toward manual investigation.
    """
    text = _ticket_text(ticket)
    lower = text.lower()
    amount, _ = extract_refund_request(ticket)
    currency = _detect_currency(text)

    is_duplicate = any(h in lower for h in _DUP_HINTS)
    refund_type = "duplicate_payment" if is_duplicate else ("product_return" if amount > 0 else "unknown")

    order_match = _ORDER_RE.search(text)
    txn_match = _TXN_RE.search(text)
    custom = ticket.get("custom_fields") or {}

    # Verification checklist — True only for what the ticket actually proves.
    verification = {
        "customer": bool(ticket.get("requester_id")),
        "ticket": bool(ticket.get("id")),
        "order_id": bool(order_match or custom.get("cf_reference_number")),
        "transaction_id": bool(txn_match),
        "amount": amount > 0,
        "currency": bool(currency),
        # These require a payment-system lookup the MCP server can't do — so
        # they stay unverified and (correctly) push duplicates to investigation.
        "payment_status": False,
        "existing_refund": False,
    }

    return {
        "refund_type": refund_type,
        "amount": amount,
        "currency": currency,
        "order_id": order_match.group(1) if order_match else None,
        "transaction_id": txn_match.group(1) if txn_match else None,
        "verification": verification,
    }


def _refund_process_plan(a) -> list[str]:
    """The concrete steps the agent will run once a human authorizes."""
    return [
        f"1. Authorize refund of {a.refund_amount} {a.currency} to the original payment method.",
        "2. [STUB] Payment processor (Stripe/Razorpay) — integration point, no money moved yet.",
        "3. Reply to the customer that the refund is approved and processing (5-7 business days).",
        "4. Resolve the ticket and tag it 'refund-approved'.",
        "5. Log the refund reference for the finance audit trail.",
    ]


def _format_decision_note(ticket: dict, signals: dict, a, policies: list[dict]) -> str:
    """
    Renders the agent's full reasoning pipeline so the human reviewer sees
    every step, the proposed refund process, and can then approve.
    """
    policy_titles = [p["title"] for p in policies] or ["(none loaded)"]
    ok = "DONE"

    lines = [
        "REFUND AGENT — PROCESS LOG",
        "=" * 40,
        f"[{ok}] Understanding request      -> refund request read from ticket {ticket.get('id')}",
        f"[{ok}] Searching knowledge        -> loaded {len(policies)} KB policies",
    ]
    lines += [f"          - {t}" for t in policy_titles]
    lines += [
        f"[{ok}] Retrieving context         -> customer={ticket.get('requester_id')}, "
        f"order={signals.get('order_id') or 'n/a'}, txn={signals.get('transaction_id') or 'n/a'}, "
        f"amount={a.refund_amount} {a.currency}",
        f"[{ok}] Checking policy & deciding -> type={a.refund_type}, "
        f"verified_duplicate={a.verified_duplicate}, eligibility={a.eligibility}",
        f"[{ok}] Selecting skill & tool     -> Freshdesk MCP (fetchTicket/replyTicket/updateTicket)",
    ]
    for r in a.reasons:
        lines.append(f"          reason: {r}")
    if a.missing:
        lines.append(f"          unverified fields: {', '.join(a.missing)}")

    lines += [
        "",
        f"PROPOSED REFUND PROCESS (runs after approval) — approver: {a.approver}",
        "-" * 40,
    ]
    lines += _refund_process_plan(a)

    lines += [
        "",
        "[WAITING] Human approval          -> pending your authorization",
        "[ ]       Refund action authorized -> then payment processor (integration point)",
        "[ ]       Customer notified        -> ticket resolved + tagged",
        "[ ]       Completed",
        "",
        '>> TO APPROVE: add a private note containing the word "Approved".',
        ">> The agent will then execute the process above and complete the refund.",
    ]
    return "\n".join(lines)


# Tags a prior agent run leaves that mean "this ticket is awaiting a human".
AWAITING_APPROVAL_TAGS = {"refund-eligible", "refund-needs-review", "manual-investigation"}
# Tag set once the refund is finalized, so re-fired webhooks don't double-act.
FINALIZED_TAG = "refund-approved"


def _tags_for(approver: str, eligibility: str) -> list[str]:
    if eligibility == "eligible_pending_approval":
        return ["refund-eligible", f"approve-{approver.lower().replace(' ', '-')}", "ai-analyzed"]
    return ["refund-needs-review", "manual-investigation", "ai-analyzed"]


async def _assess_and_route(session, ticket: dict, ticket_id: int) -> None:
    """First pass: investigate, classify, post decision note, route to a human."""
    policies = await fetch_refund_policies(session)
    log.info("Ticket %s: loaded %d KB policy articles: %s",
             ticket_id, len(policies), [p["title"] for p in policies])

    # Try the AI (Nova) first — it reasons over the ticket + policies and drafts
    # a customer reply. If it fails, fall back to the rule-based engine.
    ai = analyze_refund(ticket, policies)

    if ai:
        approver = ai.get("approver", "Manual investigation")
        eligibility = ai.get("eligibility", "needs_investigation")
        log.info("Ticket %s: AI -> type=%s amount=%s %s (approver=%s)",
                 ticket_id, ai.get("refund_type"), ai.get("amount"), eligibility, approver)

        note = "\n".join([
            "REFUND AGENT (AI / Amazon Nova) — DECISION",
            "=" * 40,
            f"Refund type: {ai.get('refund_type')}",
            f"Amount: {ai.get('amount')} {ai.get('currency', '')}",
            f"Eligibility: {eligibility}",
            f"Required approver: {approver}",
            "",
            f"AI reasoning: {ai.get('reasoning', '')}",
            "",
            "Drafted customer reply (sent):",
            f"  {ai.get('customer_reply', '')}",
            "",
            'To release the refund, add a private note containing "Approved".',
            "The agent does not move money; a human must approve.",
        ])
        await call_tool(session, "createTicketNote", {"id": ticket_id, "body": note, "private": True})

        # Send the AI-written reply to the customer immediately.
        reply = ai.get("customer_reply")
        if reply:
            await call_tool(session, "replyTicket", {"id": ticket_id, "body": reply})

        tags = _tags_for(approver, eligibility)
        update = {"id": ticket_id, "status": PENDING_STATUS, "tags": tags}
        if FINANCE_GROUP_ID > 0:
            update["group_id"] = FINANCE_GROUP_ID
        await call_tool(session, "updateTicket", update)
        return

    # --- Fallback: rule-based engine (AI unavailable) ---
    signals = build_refund_signals(ticket)
    assessment = assess_refund(signals)
    log.info("Ticket %s: RULES -> type=%s amount=%s -> %s (approver=%s)",
             ticket_id, assessment.refund_type, assessment.refund_amount,
             assessment.eligibility, assessment.approver)

    note = _format_decision_note(ticket, signals, assessment, policies)
    await call_tool(session, "createTicketNote", {"id": ticket_id, "body": note, "private": True})

    if assessment.eligibility == "eligible_pending_approval":
        tags = ["refund-eligible", f"approve-{assessment.approver.lower().replace(' ', '-')}"]
    else:
        tags = ["refund-needs-review", "manual-investigation"]

    update = {"id": ticket_id, "status": PENDING_STATUS, "tags": tags}
    if FINANCE_GROUP_ID > 0:
        update["group_id"] = FINANCE_GROUP_ID
    await call_tool(session, "updateTicket", update)


async def _finalize_after_approval(session, ticket: dict, ticket_id: int, approval_note: dict) -> None:
    """Second pass: a human wrote 'Approved' — reply to the customer and resolve."""
    log.info("Ticket %s: human approval detected (note %s) — finalizing refund",
             ticket_id, approval_note.get("id"))

    completion_note = "\n".join([
        "REFUND AGENT — PROCESS LOG (CONTINUED)",
        "=" * 40,
        f"[DONE] Human approval received   -> refund action authorized via note {approval_note.get('id')}",
        "[STUB] Payment processor call    -> integration point ready (no money moved)",
        "[DONE] Customer notified         -> ticket resolved + tagged refund-approved",
        "[DONE] Completed",
    ])
    await call_tool(session, "createTicketNote", {"id": ticket_id, "body": completion_note, "private": True})
    # Freshdesk MCP never moves money; call your payment processor here.
    # await issue_refund(ticket)

    # AI writes the approval confirmation to the customer (falls back to a
    # fixed message if Nova is unavailable).
    amount, _ = extract_refund_request(ticket)
    reply = write_approval_reply(ticket, amount or None)
    await call_tool(session, "replyTicket", {"id": ticket_id, "body": reply})

    tags = list({*(ticket.get("tags") or []), FINALIZED_TAG})
    await call_tool(session, "updateTicket", {"id": ticket_id, "status": RESOLVED_STATUS, "tags": tags})


async def process_refund_ticket(ticket_id: int) -> None:
    async with freshdesk_mcp_session() as session:
        ticket = await call_tool(session, "fetchTicket", {"id": ticket_id})
        tags = set(ticket.get("tags") or [])

        # Already finalized? Do nothing (a re-fired webhook must not double-refund).
        if FINALIZED_TAG in tags:
            log.info("Ticket %s: already finalized (%s) — skipping", ticket_id, FINALIZED_TAG)
            return

        # If this ticket is awaiting a human and one wrote "Approved", finalize.
        if tags & AWAITING_APPROVAL_TAGS:
            conversations = await fetch_conversations(session, ticket_id)
            approval = find_human_approval(conversations)
            if approval:
                await _finalize_after_approval(session, ticket, ticket_id, approval)
                return
            log.info("Ticket %s: awaiting approval, no 'Approved' note yet — nothing to do", ticket_id)
            return

        # Fresh ticket: run the first-pass assessment.
        await _assess_and_route(session, ticket, ticket_id)


# In Lambda there is no event loop after the response is returned, so we must
# finish processing in-request. Locally (uvicorn) we can fire-and-forget.
RUNNING_IN_LAMBDA = bool(os.environ.get("AWS_LAMBDA_FUNCTION_NAME"))


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/webhooks/freshdesk/refund")
async def refund_webhook(request: Request):
    payload = await request.json()
    ticket_id = payload.get("ticket_id") or payload.get("id")
    if not ticket_id:
        return {"ok": False, "error": "no ticket_id in payload"}

    if RUNNING_IN_LAMBDA:
        # Process synchronously — the container is frozen after we return.
        try:
            await process_refund_ticket(int(ticket_id))
        except Exception:  # noqa: BLE001 — log and still ack so Freshdesk stops retrying
            log.exception("Ticket %s: processing failed", ticket_id)
            return {"ok": False, "ticket_id": ticket_id, "error": "processing failed"}
        return {"ok": True, "ticket_id": ticket_id, "processed": True}

    # Local: fire-and-forget so uvicorn returns immediately.
    asyncio.create_task(process_refund_ticket(int(ticket_id)))
    return {"ok": True, "ticket_id": ticket_id}


# --- Scheduled poller (EventBridge) ----------------------------------------
#
# Runs on a timer instead of a Freshdesk webhook. Finds refund tickets that
# still need action and feeds each to process_refund_ticket (which itself
# decides assess vs finalize vs skip).

# Freshdesk statuses we still care about: 2 Open, 3 Pending. 4/5 are done.
_ACTIONABLE_STATUSES = {2, 3}


def _looks_like_refund(ticket: dict) -> bool:
    text = f"{ticket.get('subject', '')} {ticket.get('description_text') or ticket.get('description', '')}".lower()
    return "refund" in text or "charged twice" in text or "duplicate" in text


async def poll_refund_tickets(max_tickets: int = 25) -> dict:
    """
    Scan recent tickets and process any refund ticket that needs action:
      - new refund tickets with no agent tags -> assess
      - pending tickets awaiting approval with an "Approved" note -> finalize
    Returns a small summary for logging.
    """
    processed, skipped = [], 0
    async with freshdesk_mcp_session() as session:
        listing = await call_tool(session, "fetchTickets", {"per_page": max_tickets})
        tickets = listing if isinstance(listing, list) else (listing.get("results") or [])

        for t in tickets:
            tid = t.get("id")
            tags = set(t.get("tags") or [])
            status = t.get("status")

            if FINALIZED_TAG in tags:
                skipped += 1
                continue
            if status not in _ACTIONABLE_STATUSES:
                skipped += 1
                continue
            if not _looks_like_refund(t):
                skipped += 1
                continue

            # Needs assessment (no agent tags yet) OR might have an approval note.
            needs_assessment = not (tags & AWAITING_APPROVAL_TAGS)
            awaiting = bool(tags & AWAITING_APPROVAL_TAGS)
            if not (needs_assessment or awaiting):
                skipped += 1
                continue

            try:
                await process_refund_ticket(int(tid))
                processed.append(tid)
            except Exception:  # noqa: BLE001
                log.exception("Poller: ticket %s failed", tid)

    summary = {"processed": processed, "skipped": skipped}
    log.info("Poller run: %s", summary)
    return summary


# AWS Lambda entrypoint. Routes EventBridge scheduled events to the poller and
# API Gateway HTTP events to the FastAPI app via Mangum.
try:
    from mangum import Mangum

    # lifespan="off": we have no startup/shutdown events, and enabling it makes
    # Mangum call asyncio.get_event_loop(), which fails on a warm container after
    # the poller path has used its own loop.
    _asgi_handler = Mangum(app, lifespan="off")

    def _run_poller() -> dict:
        # Use a fresh loop so we never close/replace the default one that Mangum
        # may reuse on a later warm invocation.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(poll_refund_tickets())
        finally:
            loop.close()

    def handler(event, context):
        # Scheduled invocations come from EventBridge (no HTTP request shape).
        if isinstance(event, dict) and (
            event.get("source") == "aws.events"
            or event.get("detail-type") == "Scheduled Event"
            or event.get("poll") is True
        ):
            return {"ok": True, **_run_poller()}
        # Otherwise treat it as an HTTP (API Gateway) event.
        return _asgi_handler(event, context)

except ImportError:  # mangum only needed in Lambda; local runs don't import it
    handler = None


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
