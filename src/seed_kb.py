"""
Seed the ddretail Freshdesk knowledge base with the Oak & Loom refund
policies the agent uses as decision context.

Creates: Solution Category -> Folder -> 4 articles.

Run: python seed_kb.py
"""

import asyncio
import json

from mcp_client import call_tool, freshdesk_mcp_session

CATEGORY_NAME = "Refunds and Payments"
FOLDER_NAME = "Refund Policies"

ARTICLES = [
    {
        "title": "Duplicate Payment Refund Policy",
        "description": """<h2>Purpose</h2>
<p>This policy defines when Oak &amp; Loom Furniture customers qualify for a refund when they are charged more than once for the same order.</p>
<h2>Eligibility</h2>
<p>A customer qualifies when:</p>
<ul>
<li>Both payments belong to the same order.</li>
<li>Both transactions are successful.</li>
<li>Both transactions have the same customer.</li>
<li>The duplicate payment has not already been refunded.</li>
</ul>
<h2>Refund Amount</h2>
<p>The refund amount is the confirmed duplicate charge, not the original legitimate payment.</p>
<p><strong>Example:</strong><br>
Order total: $4,800<br>
Payment 1: $4,800 - Successful<br>
Payment 2: $4,800 - Successful<br>
Eligible refund: $4,800</p>
<h2>Approval</h2>
<p>Duplicate-payment refunds require human approval before execution.</p>
<p><strong>Required verification:</strong> Customer, ticket, order, payment transactions, payment status and previous refund history.</p>""",
    },
    {
        "title": "Furniture Returns and Refund Policy",
        "description": """<p>Furniture returns are normally allowed within 30 days of delivery for eligible products.</p>
<ul>
<li>Custom or personalized furniture requires manual review.</li>
<li>Products damaged after delivery by the customer require manual investigation.</li>
</ul>
<p>A duplicate-payment issue is not treated as a product return.</p>
<p>When a customer was charged twice for the same order, use the <strong>Duplicate Payment Refund Policy</strong> instead.</p>
<p>The agent must verify the customer's order and payment information before creating a refund request.</p>""",
    },
    {
        "title": "Payment Verification - Duplicate Charge Investigation",
        "description": """<p>Before classifying a payment as a duplicate, verify:</p>
<ul>
<li>Customer identity</li>
<li>Freshdesk ticket</li>
<li>Company/account</li>
<li>Order ID</li>
<li>Transaction ID</li>
<li>Payment amount</li>
<li>Currency</li>
<li>Payment status</li>
<li>Payment timestamp</li>
<li>Existing refund status</li>
</ul>
<p>A payment is considered a confirmed duplicate when:</p>
<p><strong>Same Customer + Same Order + Same Amount + Same Currency + Both Successful + No Existing Refund</strong></p>
<p>If any required condition cannot be verified, do not automatically create the refund request. Escalate for manual investigation.</p>""",
    },
    {
        "title": "Refund Approval and Authorization Policy",
        "description": """<p>KnowFlow may investigate a refund request and create a refund request, but it must not execute a refund without the required human approval.</p>
<h2>Approval rules</h2>
<ul>
<li>Duplicate refund &le; $500 -&gt; Finance approval</li>
<li>Duplicate refund &gt; $500 -&gt; Finance Manager approval</li>
<li>Product refund &le; $1,000 -&gt; Support approval</li>
<li>Product refund &gt; $1,000 -&gt; Finance approval</li>
<li>Unverified payment -&gt; Manual investigation</li>
</ul>
<p>Before requesting approval, KnowFlow must display:</p>
<p>Customer, Freshdesk ticket, order, product, original payment, duplicate payment, refund amount, applicable policies and eligibility decision.</p>""",
    },
]


async def main() -> None:
    async with freshdesk_mcp_session() as session:
        # Some MCP tools want a conversation handle + _reasoning. Start one.
        conv = await call_tool(session, "start_conversation", {})
        conv_id = conv.get("conversation_id") or conv.get("id") or ""
        print("conversation:", json.dumps(conv)[:300])

        base = {"conversation_id": conv_id, "_reasoning": "Seeding refund policy KB articles requested by admin"}

        cat = await call_tool(
            session,
            "createSolutionCategory",
            {**base, "name": CATEGORY_NAME, "description": "Refund and payment policies for Oak and Loom Furniture."},
        )
        cat_id = cat.get("id")
        print("category id:", cat_id)

        folder = await call_tool(
            session,
            "createSolutionCategoryFolder",
            {**base, "id": cat_id, "name": FOLDER_NAME, "description": "Policies the refund agent uses as context.", "visibility": 2},
        )
        folder_id = folder.get("id")
        print("folder id:", folder_id)

        for art in ARTICLES:
            created = await call_tool(
                session,
                "createSolutionFolderArticle",
                {**base, "id": folder_id, "title": art["title"], "description": art["description"], "status": 2},
            )
            print("article:", created.get("id"), "-", created.get("title"))


if __name__ == "__main__":
    asyncio.run(main())
