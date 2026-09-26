"""
Refund eligibility policy. Pure logic, no Freshdesk calls — easy to unit test
and to tune independently of the MCP plumbing.
"""

import os
from dataclasses import dataclass

AUTO_APPROVE_LIMIT = float(os.environ.get("REFUND_AUTO_APPROVE_LIMIT", "2000"))
MAX_ORDER_AGE_DAYS = int(os.environ.get("REFUND_MAX_ORDER_AGE_DAYS", "30"))
HIGH_PRIORITY_THRESHOLD = 4  # Freshdesk priority: 1 Low .. 4 Urgent


@dataclass
class RefundDecision:
    approved: bool
    reason: str


def evaluate_refund(ticket: dict, requested_amount: float, order_age_days: int) -> RefundDecision:
    if requested_amount <= 0:
        return RefundDecision(False, "No refund amount found on the ticket — needs a human to read it")

    if requested_amount > AUTO_APPROVE_LIMIT:
        return RefundDecision(
            False,
            f"Amount {requested_amount} exceeds auto-approve limit of {AUTO_APPROVE_LIMIT}",
        )

    if order_age_days > MAX_ORDER_AGE_DAYS:
        return RefundDecision(
            False,
            f"Order is {order_age_days} days old, outside the {MAX_ORDER_AGE_DAYS}-day window",
        )

    if ticket.get("priority", 1) >= HIGH_PRIORITY_THRESHOLD:
        return RefundDecision(False, "Ticket is marked Urgent — routed to a human")

    return RefundDecision(True, "Within policy: amount and order age both check out")


# --- Duplicate-payment + approval-tier logic (KB-driven) -------------------
#
# Mirrors the seeded KB articles:
#   - Duplicate Payment Refund Policy
#   - Payment Verification (Duplicate Charge Investigation)
#   - Refund Approval and Authorization Policy
#
# The agent NEVER executes money movement and NEVER auto-approves here — it
# investigates, classifies, and routes to the correct human approver.

# Approval tiers from "Refund Approval and Authorization Policy".
DUPLICATE_FINANCE_LIMIT = 500.0   # <= 500 duplicate -> Finance; above -> Finance Manager
PRODUCT_SUPPORT_LIMIT = 1000.0    # <= 1000 product  -> Support; above -> Finance


@dataclass
class RefundAssessment:
    refund_type: str          # "duplicate_payment" | "product_return" | "unknown"
    verified_duplicate: bool
    refund_amount: float
    currency: str
    approver: str             # who must approve, per the approval policy
    eligibility: str          # "eligible_pending_approval" | "needs_investigation"
    reasons: list[str]
    missing: list[str]        # verification fields we could not confirm


# The verification checklist from "Payment Verification".
DUPLICATE_VERIFICATION_FIELDS = [
    "customer",
    "ticket",
    "order_id",
    "transaction_id",
    "amount",
    "currency",
    "payment_status",
    "existing_refund",
]


def _duplicate_approver(amount: float) -> str:
    return "Finance" if amount <= DUPLICATE_FINANCE_LIMIT else "Finance Manager"


def _product_approver(amount: float) -> str:
    return "Support" if amount <= PRODUCT_SUPPORT_LIMIT else "Finance"


def assess_refund(signals: dict) -> RefundAssessment:
    """
    Classify and route a refund request using the KB policy rules.

    `signals` is a normalized dict extracted from the ticket + context, e.g.:
      {
        "refund_type": "duplicate_payment",
        "amount": 4800.0, "currency": "USD",
        "verification": {"customer": True, "order_id": True, ...},
      }
    """
    refund_type = signals.get("refund_type", "unknown")
    amount = float(signals.get("amount") or 0)
    currency = signals.get("currency") or "USD"
    verification = signals.get("verification") or {}
    reasons: list[str] = []

    if refund_type == "duplicate_payment":
        missing = [f for f in DUPLICATE_VERIFICATION_FIELDS if not verification.get(f)]
        verified = not missing and amount > 0

        if not verified:
            reasons.append(
                "Duplicate charge could not be fully verified — Payment Verification policy "
                "requires same customer, order, amount, currency, both successful, no existing refund."
            )
            return RefundAssessment(
                refund_type="duplicate_payment",
                verified_duplicate=False,
                refund_amount=amount,
                currency=currency,
                approver="Manual investigation",
                eligibility="needs_investigation",
                reasons=reasons,
                missing=missing,
            )

        approver = _duplicate_approver(amount)
        reasons.append(
            f"Confirmed duplicate charge of {amount} {currency}. Refund amount is the duplicate charge, "
            f"not the original payment. Requires {approver} approval before execution."
        )
        return RefundAssessment(
            refund_type="duplicate_payment",
            verified_duplicate=True,
            refund_amount=amount,
            currency=currency,
            approver=approver,
            eligibility="eligible_pending_approval",
            reasons=reasons,
            missing=[],
        )

    if refund_type == "product_return":
        if amount <= 0:
            reasons.append("Product return with no confirmed amount — needs investigation.")
            return RefundAssessment(
                "product_return", False, amount, currency, "Manual investigation",
                "needs_investigation", reasons, ["amount"],
            )
        approver = _product_approver(amount)
        reasons.append(f"Product refund of {amount} {currency}. Requires {approver} approval per policy.")
        return RefundAssessment(
            "product_return", False, amount, currency, approver,
            "eligible_pending_approval", reasons, [],
        )

    reasons.append("Refund type could not be determined from the ticket — routing to manual investigation.")
    return RefundAssessment(
        "unknown", False, amount, currency, "Manual investigation",
        "needs_investigation", reasons, ["refund_type"],
    )
