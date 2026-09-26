"""
AI layer for the refund agent — Amazon Nova Lite via the Bedrock Converse API.

The model reads the customer's ticket and the refund policy KB articles, then
returns a structured decision (refund type, amount, eligibility, approver) plus
a natural, customer-facing reply. It never moves money — money-movement still
requires a human "Approved" note downstream.
"""

import json
import logging
import os

import boto3

log = logging.getLogger("refund-agent.ai")

MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.amazon.nova-lite-v1:0")
REGION = os.environ.get("AWS_REGION", "us-east-1")

_bedrock = None


def _client():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client("bedrock-runtime", region_name=REGION)
    return _bedrock


SYSTEM_PROMPT = """You are a refund support agent for Oak & Loom Furniture.
You read a customer's support ticket and the company's refund policies, then decide how to handle the refund.

Rules you must follow:
- You may investigate and recommend, but you must NEVER approve or execute a refund yourself. A human approves money movement.
- Classify the refund as one of: "duplicate_payment", "product_return", or "unknown".
- Duplicate payment: refund the duplicate charge only; needs Finance approval if <= $500, Finance Manager if > $500.
- Product return: needs Support approval if <= $1000, Finance if > $1000.
- If key facts (payment status, existing refund, amount) cannot be verified from the ticket, set eligibility to "needs_investigation".
- Be warm, concise, and professional in the customer reply. Do not promise the refund is done — say it is being reviewed/processed pending approval.

Respond with ONLY a JSON object, no other text, in exactly this shape:
{
  "refund_type": "duplicate_payment | product_return | unknown",
  "amount": <number or 0>,
  "currency": "<e.g. USD or INR>",
  "eligibility": "eligible_pending_approval | needs_investigation",
  "approver": "Finance | Finance Manager | Support | Manual investigation",
  "reasoning": "<one or two sentences on why>",
  "customer_reply": "<the message to send to the customer>"
}"""


def analyze_refund(ticket: dict, policies: list[dict]) -> dict | None:
    """
    Ask Nova to classify the refund and draft a customer reply.
    Returns the parsed dict, or None if the AI call/parse fails (caller falls
    back to the rule-based path).
    """
    subject = ticket.get("subject", "")
    body = ticket.get("description_text") or ticket.get("description", "")
    policy_text = "\n\n".join(f"### {p['title']}\n{p['body']}" for p in policies)

    user_msg = (
        f"CUSTOMER TICKET\nSubject: {subject}\nBody: {body}\n\n"
        f"REFUND POLICIES\n{policy_text}\n\n"
        "Decide how to handle this refund and write the customer reply. Respond with only the JSON object."
    )

    try:
        resp = _client().converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{"text": user_msg}]}],
            inferenceConfig={"maxTokens": 700, "temperature": 0.2},
        )
        text = resp["output"]["message"]["content"][0]["text"].strip()
        # Nova sometimes wraps JSON in ```json fences — strip them.
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{") : text.rfind("}") + 1]
        return json.loads(text)
    except Exception:  # noqa: BLE001 — any failure -> caller uses rule-based fallback
        log.exception("Nova refund analysis failed; falling back to rules")
        return None


APPROVAL_SYSTEM_PROMPT = """You are a refund support agent for Oak & Loom Furniture.
A refund has just been APPROVED by a human and is now being processed.
Write a short, warm, professional message to the customer confirming the refund is approved and processing.
- Mention the order and amount if available.
- Say it will reflect in 5-7 business days.
- Do NOT ask for more information. Keep it to 2-3 sentences.
Respond with ONLY the message text, no JSON, no preamble."""


def write_approval_reply(ticket: dict, amount=None, currency: str = "") -> str:
    """
    Nova writes the final 'refund approved' customer reply. Falls back to a
    fixed message if the AI call fails.
    """
    subject = ticket.get("subject", "")
    body = ticket.get("description_text") or ticket.get("description", "")
    detail = f"Approved amount: {amount} {currency}." if amount else ""

    try:
        resp = _client().converse(
            modelId=MODEL_ID,
            system=[{"text": APPROVAL_SYSTEM_PROMPT}],
            messages=[{"role": "user", "content": [{
                "text": f"Ticket subject: {subject}\nCustomer wrote: {body}\n{detail}\nWrite the approval confirmation."
            }]}],
            inferenceConfig={"maxTokens": 250, "temperature": 0.3},
        )
        return resp["output"]["message"]["content"][0]["text"].strip()
    except Exception:  # noqa: BLE001
        log.exception("Nova approval reply failed; using fallback text")
        return (
            "Good news — your refund has been approved and is now being processed. "
            "You should see it reflected within 5-7 business days. Thanks for your patience."
        )
