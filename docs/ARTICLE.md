# Building an AI Refund Agent on Freshdesk + AWS

*How I turned refund emails into an autonomous, AI-powered workflow that reads,
reasons, replies, and routes to a human — with the money-movement step safely
gated behind human approval.*

---



## Problem statement

Refund requests are high-volume, repetitive, and easy to get wrong. A support
team member has to:

1. Read the customer's email and figure out what they actually want.
2. Work out whether it's a duplicate charge or a product return.
3. Find the amount and order details buried in free-text.
4. Check it against company policy.
5. Decide who needs to approve it.
6. Reply to the customer.
7. Route the ticket to the right team.

That's minutes of manual triage per ticket, inconsistent decisions between
agents, and slow first responses for customers. The refund *amount* is rarely a
tidy form field — it's in a sentence like *"I was billed twice, two charges of
$320 each, please return the extra one."*

**Goal:** an agent that does the triage automatically, explains its reasoning,
replies to the customer, and only asks a human for the one thing that truly
needs a human — approving the money movement.

---

## What I built

An autonomous refund agent that lives entirely on AWS and integrates with
Freshdesk through the Freshdesk Product MCP server.

The flow:

1. A customer emails a refund request; Freshdesk creates a ticket.
2. A Freshdesk automation rule calls an API Gateway webhook, which invokes an
   AWS Lambda that reads the ticket and the knowledge-base policies. (An
   EventBridge poller was the first approach — see "What I learned".)
3. **Amazon Nova** (via Bedrock) classifies the refund, extracts the amount and
   order, applies the approval tiers, and drafts a customer reply.
4. The agent posts its reasoning as a private note and sends the AI-written reply
   to the customer. The ticket goes Pending.
5. A human reviews and adds a private note containing **"Approved"**.
6. The agent detects the approval, writes an AI confirmation to the customer,
   and resolves the ticket.

The agent never moves money on its own. Human approval is a hard gate.

See `docs/architecture.drawio` for the full diagram.

---

## Track fit and scope

**Track 2 — Platform: Agent Skills & Knowledge.** This is a reusable Freshworks
platform skill: it uses the Freshdesk Product MCP server as its tool interface
and the Freshdesk Solutions knowledge base as its policy knowledge, so the
business logic lives in Freshworks and the agent reasons over it — rather than a
one-off script with hardcoded rules.

**What it does:** automate refund triage end-to-end — classify the request,
apply KB policy, reply to the customer, and route to a human for authorization.

**What it doesn't do (out of scope):**

- Move money — payment execution is a marked `[STUB]`; a human "Approved" note
  is a hard gate.
- Make the final financial decision — the AI recommends, the human authorizes.
- Act as a general support chatbot — refund/duplicate-charge intents only.
- Manage Freshdesk configuration beyond the tickets and KB it uses.
- Verify external payment records — unverifiable duplicates go to manual review.

**Product integrations:**

- **Freshdesk (Freshworks)** — channel, ticket store, KB policies, automation
  rule trigger, private notes, public replies, tags/status.
- **Freshdesk Product MCP** — the agent's tool access to Freshdesk.
- **Amazon Bedrock / Nova Lite** — reasoning + customer replies.
- **AWS Lambda / API Gateway / Secrets Manager / EventBridge / CloudWatch** —
  runtime, webhook endpoint, secret storage, fallback schedule, logs.

---

## Technology stack

| Layer | Choice | Why |
|-------|--------|-----|
| Helpdesk | **Freshdesk** | Tickets, contacts, and a Solutions KB the agent uses as context |
| Integration | **Freshdesk Product MCP** | Tenant-scoped, wraps REST v2, API-key auth — the same tool protocol AI agents speak |
| AI | **Amazon Bedrock — Nova Lite** | Fast, low-cost reasoning + natural-language replies |
| Compute | **AWS Lambda (Python 3.13)** | Serverless, no server to run, scales to zero |
| Trigger | **API Gateway (HTTP API)** | Freshdesk webhook — fires only on real refunds |
| Scheduling | **Amazon EventBridge** | Polling fallback, kept disabled (drained the action cap) |
| Secrets | **AWS Secrets Manager** | API key encrypted, read at runtime, never in code |
| Web framework | **FastAPI + Mangum** | ASGI app locally; Mangum adapts it to Lambda |
| Observability | **CloudWatch Logs** | Every decision and reply is logged |
| Fallback engine | **Pure-Python rules (`policy.py`)** | Deterministic backup if the AI is unavailable |

---

## Why AWS for Freshdesk

Freshdesk holds the tickets, but it can't *run your logic*. AWS is where the
agent's brain lives and executes.

```
Freshdesk = the front desk (tickets, customer emails, replies)
AWS       = the back office (runs the code, reasons, recommends, keeps secrets)
MCP       = the phone line between them
```

Freshdesk can store tickets, fire a webhook, and show replies — but it cannot
run Python, call an AI model you control, or hold secrets securely. Something
has to receive the webhook and do the work; that is AWS.

| Need | AWS service | Why not Freshdesk |
|------|-------------|-------------------|
| Run the agent code (Python) | Lambda | No place in Freshdesk to run a program |
| A public URL Freshdesk can call | API Gateway | Freshdesk needs an internet endpoint to POST to |
| The AI brain (reasoning + replies) | Bedrock / Nova | Freshdesk has no LLM you control |
| Store the API key safely | Secrets Manager | The key can't sit in code or a file |
| See what the agent did | CloudWatch | Debugging and audit logs |

Alternatives considered: running on a laptop (dies on sleep, needs a tunnel);
Freshdesk's own automations (can send a canned reply, but no AI reasoning or
tiered approvals); another cloud (Azure/GCP would also work). AWS won because it
has every piece — compute, AI, secrets, endpoint — in one place. AWS isn't
decorative; it's the engine, while Freshdesk is the customer-facing surface.

---

## How Freshdesk helps the customer

Freshdesk is the customer-facing surface, and it's doing a lot of the heavy
lifting the agent depends on:

- **Email-to-ticket**: the customer just emails support; Freshdesk turns it into
  a structured ticket with a requester, subject, and body. No portal, no form.
- **Solutions / Knowledge Base as the policy source**: the four refund policies
  live as KB articles. The agent reads them as its decision context, so the
  rules live in Freshdesk where the business owns them — not hardcoded.
- **Private notes vs public replies**: the agent posts its reasoning as *private*
  notes (internal only) and customer messages as *public* replies. The customer
  sees a clean, helpful conversation; the team sees the full audit trail.
- **Tags and status**: `refund-eligible`, `approve-finance`, `ai-analyzed`,
  `refund-approved` plus Pending/Resolved status make the queue self-organizing
  and reportable.
- **Human-in-the-loop**: approval is just a private note with "Approved" — no new
  tool for the agent to learn. The refund stays inside the Freshdesk workflow.

The result for the customer: a fast, accurate first response that references
their real order and amount, and a clear confirmation once approved.

---

## Strong points

- **Real AI reasoning, not keyword matching.** Nova understands
  "double charged", "billed twice", "return the extra one" — phrasings a
  keyword rule would miss — and pulls the amount and order id from prose.
- **Policy-driven, business-owned.** Change a KB article and the agent's
  behavior changes. No redeploy needed to tune policy language.
- **Safe by design.** The agent investigates and recommends; a human approves
  money. Idempotency tagging prevents double-refunds on repeated runs.
- **Resilient.** If Bedrock is unavailable, it falls back to a deterministic
  rule engine, so it never goes dark.
- **Zero-ops hosting.** Serverless — nothing to keep running, no ngrok, scales
  to zero when idle.
- **Auditable.** Every decision is a private note plus a CloudWatch log line.

---

## How I utilised each piece

- **MCP** to read/write Freshdesk (`fetchTicket`, `fetchSolutionFolderArticles`,
  `createTicketNote`, `replyTicket`, `updateTicket`, `fetchTicketConversations`).
- **Nova** for two jobs: (1) structured decision JSON (type, amount, tier,
  eligibility, reasoning), and (2) natural customer replies — both the initial
  acknowledgment and the post-approval confirmation.
- **Secrets Manager** so the Freshdesk API key is never in code or env files in
  the cloud; the Lambda reads it at runtime with a scoped IAM permission.
- **EventBridge** to make it autonomous without touching Freshdesk automation
  rules (which aren't exposed via the API).
- **Lambda + Mangum** so the same FastAPI app runs locally as a server and in
  the cloud as a function.
- **A rule engine** kept as a safety net and as the original, explainable logic.

---

## What I learned

- **Windows vs Lambda dependencies.** The `mcp` library pulls in `pywin32` on
  Windows. Building the Lambda zip locally kept trying to include a Windows-only
  wheel. The fix was to install Linux wheels explicitly
  (`pip --platform manylinux2014_x86_64 --only-binary=:all:`) rather than fight
  Docker builds.
- **MCP protocol versions matter.** An older bundled `mcp` (1.10.1) rejected
  Freshdesk's protocol version `2025-11-25`. Pinning `mcp==1.28.1` fixed it.
  Lesson: match the client library version to what the server negotiates.
- **Lambda has no event loop after you return.** The original fire-and-forget
  `asyncio.create_task` pattern silently dropped work in Lambda. And calling
  `asyncio.run()` in the poller path closed the default loop, which then broke
  Mangum on the next warm invocation. The fix: process synchronously in Lambda,
  run the poller in its own fresh loop, and disable Mangum's lifespan.
- **Public Lambda Function URLs can be blocked** by account guardrails — the URL
  returned 403 despite a correct policy. API Gateway was the reliable path.
- **AI + rules is better than either alone.** The LLM handles messy language;
  the rules provide a deterministic, explainable fallback and guardrails.
- **Keep money movement out of the AI's hands.** The single most important design
  decision was making human approval a hard gate.
- **Polling burned the vendor quota — events are cheaper.** My first "make it
  automatic" approach was a 2-minute EventBridge poller (because Freshdesk
  automation rules can't be created via API). It ran ~720 times a day and spent
  Freshdesk MCP actions on every run, even with zero refunds — it exhausted the
  1,000-action monthly cap in about two days and Freshdesk paused the connection,
  so a real customer email got no reply. The fix was to switch to an
  event-driven webhook (a Freshdesk automation rule → API Gateway) that spends
  actions only on real refunds, plus caching the KB articles in memory so they
  aren't re-fetched per ticket. Lesson: with metered third-party APIs, prefer
  event triggers over polling, and treat the vendor's action quota as a
  first-class design constraint.

---

## Pricing (rough, low-volume)

At refund-ticket volume this runs at or near free. Indicative monthly costs:

| Service | Usage | Est. cost |
|---------|-------|-----------|
| Lambda | Per-refund invocations (webhook-driven) | Within/near free tier |
| API Gateway (HTTP) | A few requests per refund | Cents |
| EventBridge | Rule kept disabled | $0 |
| Secrets Manager | 1 secret | ~$0.40 |
| Bedrock (Nova Lite) | Analysis + customer replies, small prompts | Fractions of a cent per ticket |
| CloudWatch Logs | Low log volume | Cents |
| Freshdesk MCP | ~3–5 actions per real refund | **watch the plan's monthly action cap** |

The real constraint is **Freshdesk MCP actions**, not AWS cost. The original
2-minute poller spent actions continuously and drained a 1,000-action plan in
~2 days. The webhook design spends actions only on real refunds (~3–5 each), so
the same 1,000 actions cover roughly 200+ refunds. KB articles are also cached
in memory to avoid re-fetching them per ticket.

*Verify current numbers against AWS and Freshdesk pricing pages before relying on
them.*

---

## Conclusions

This project shows that a genuinely useful support agent doesn't need a giant
framework — it needs a clear task, the right tools, and a hard safety boundary.
Freshdesk supplies the customer channel, the structured tickets, and the policy
knowledge base. MCP gives clean tool access. Nova provides the reasoning and the
human-quality writing. AWS makes it autonomous and cheap to run.

The design principle that made it trustworthy: **let the AI do the reading,
reasoning, and drafting, but never let it move money.** A human writing
"Approved" is the entire trust boundary, and it fits naturally inside the
existing Freshdesk workflow.

Next steps for production: connect a real payment processor at the approval step,
add webhook signature verification, and consider Bedrock Guardrails on the
customer-facing replies.
