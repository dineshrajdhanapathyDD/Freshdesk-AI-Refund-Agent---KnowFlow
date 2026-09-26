# Freshdesk AI Refund Agent

*Track 2 — Platform: Agent Skills & Knowledge (reusable skills, MCP integrations, Freshworks developer platform).*

An autonomous, AI-powered refund agent that turns refund emails into an
end-to-end Freshdesk workflow — reading tickets, reasoning over knowledge-base
policies with Amazon Nova, replying to the customer, and routing to a human for
financial authorization.

## What it is

A serverless AI agent that plugs into Freshdesk through the Freshdesk Product
MCP server. It reuses the helpdesk's own building blocks — tickets, Solutions
knowledge base, automation rules, notes, replies, tags — as the agent's tools
and knowledge, and adds an AI reasoning layer (Amazon Nova on AWS) on top. The
result is a reusable refund-handling skill for Freshworks, not a bolted-on bot.

## What it does (business objective)

Cut the manual triage time and inconsistency in refund handling, and give
customers a fast, accurate first response, while keeping a human firmly in
control of the money.

1. A customer emails a refund request → Freshdesk creates a ticket.
2. The agent (on AWS Lambda) reads the ticket and the refund policy KB articles
   over the Freshdesk Product MCP server.
3. Amazon Nova classifies the refund, extracts the amount/order, determines the
   approval routing, and drafts a customer reply.
4. The agent posts its reasoning as a private note and sends the AI reply to the
   customer. The ticket goes Pending, awaiting a human.
5. A human adds a private note containing **"Approved"** to authorize.
6. The agent writes an AI confirmation to the customer and resolves the ticket.

If Nova is unavailable, the agent falls back to a rule-based engine (`policy.py`)
so it never goes dark.

## What it doesn't do (out of scope)

- **It does not move money.** Payment execution is a clearly marked `[STUB]`
  integration point; the agent authorizes routing only. A human "Approved" note
  is a hard gate.
- **It does not make the final financial decision.** The AI analyzes and
  recommends; a human authorizes.
- **It is not a general helpdesk chatbot.** It handles refund / duplicate-charge
  intents, not arbitrary support conversations.
- **It does not manage Freshdesk configuration** (agents, groups, SLAs) beyond
  reading/writing the tickets and KB it needs.
- **It does not verify external payment records** — `payment_status` /
  `existing_refund` require a payment-system lookup that is out of scope, so
  unverifiable duplicates route to manual investigation.

## Product Integrations

- **Freshdesk (Freshworks)** — customer channel, ticket system of record,
  Solutions KB (refund policies), automation rule (webhook trigger), private
  notes (reasoning/audit), public replies (customer comms), tags/status
  (workflow state).
- **Freshdesk Product MCP** — the agent's tool connection to Freshdesk (read
  tickets + KB, post notes/replies, update tickets) over REST v2 with API-key auth.
- **Amazon Bedrock — Nova Lite** — AI reasoning: classify the refund, extract
  amount/order, determine approval routing, and generate customer-facing replies.
- **AWS Lambda** — serverless agent runtime.
- **Amazon API Gateway** — public webhook endpoint Freshdesk calls (primary trigger).
- **AWS Secrets Manager** — stores the Freshdesk API key, read at runtime.
- **Amazon EventBridge** — scheduled polling fallback (kept disabled).
- **Amazon CloudWatch** — logs and audit trail.

## System Interaction Diagram

See [docs/architecture.drawio](docs/architecture.drawio) (editable) — the full
Freshdesk ↔ MCP ↔ AWS (Lambda / Nova / Secrets Manager) flow with the human
authorization loop.

**Docs:** [architecture diagram](docs/architecture.drawio) ·
[build article](docs/ARTICLE.md)

## Architecture

```
Customer email
   → Freshdesk ticket
   → Freshdesk automation rule → API Gateway webhook   (primary trigger)
        → AWS Lambda (freshdesk-refund-agent)
             → Secrets Manager (Freshdesk API key)
             → Freshdesk MCP (read ticket + KB, post notes/replies)
             → Amazon Bedrock / Nova Lite (reasoning + customer replies)
        → human adds "Approved" note → agent finalizes + resolves
```

### AWS resources (us-east-1)

| Resource | Name | Purpose |
|----------|------|---------|
| Lambda | `freshdesk-refund-agent` | Runs the agent (webhook + AI) |
| API Gateway (HTTP) | `mv31sm4tpa` | Webhook endpoint (used when an automation rule is available) |
| EventBridge rule | `freshdesk-refund-poller` | **ACTIVE trigger** — `rate(5 minutes)` polling |
| Secrets Manager | `freshdesk/refund-agent` | Freshdesk API key (encrypted) |
| Bedrock | `us.amazon.nova-lite-v1:0` | The AI model |
| IAM role | `freshdesk-refund-agent-role` | Scoped: read the secret + invoke Nova |

**Trigger design.** Two triggers are supported:

1. **Webhook (event-driven, preferred):** a Freshdesk automation rule calls the
   API Gateway webhook so the agent runs only on real refund tickets. Requires a
   working automation rule in the Freshdesk account.
2. **Poller (fallback, currently ACTIVE):** an EventBridge rule invokes the
   Lambda every 5 minutes to scan for refund tickets needing action. Used when
   the automation rule can't be configured/fired.

An earlier 2-minute poll exhausted 1,000 MCP actions in ~2 days, so the poller
now runs at `rate(5 minutes)` and the agent caches KB policies in memory to keep
action usage low. If the webhook path is set up, disable the poller to save
actions.

### Current deployment

| Setting | Value |
|---------|-------|
| Freshdesk account | `jimmathewkochittydineshraj` |
| MCP endpoint | `https://jimmathewkochittydineshraj.freshdesk.com/mcp` |
| AWS webhook | `https://mv31sm4tpa.execute-api.us-east-1.amazonaws.com/webhooks/freshdesk/refund` |
| KB policy folder id | `1120000108877` (seeded by `seed_kb.py`) |
| Region / account | `us-east-1` / `466742534146` |

Verified end-to-end on this account (deployed cloud path): a duplicate-charge
ticket was assessed by Nova (→ Finance approval, Pending), then a human
"Approved" note finalized it (→ AI confirmation reply, Resolved, no money moved).

## Why AWS for Freshdesk

Freshdesk is the customer-facing workflow and system of record. AWS provides the
compute, AI inference, secrets management, and webhook endpoint required to run
the agent.

| Role | Provided by | What it does |
|------|-------------|--------------|
| Customer workflow | **Freshdesk** | Tickets, KB policies, automation, replies |
| Agent runtime | **AWS** (Lambda + API Gateway) | Runs the code, receives the webhook |
| Reasoning | **Amazon Nova** (Bedrock) | Analyzes the request, drafts replies |
| Secrets | **AWS Secrets Manager** | Holds the Freshdesk API key |
| Tool connection | **MCP** | The agent's access to Freshdesk |

The two are complementary: Freshdesk owns the customer experience and the data;
AWS runs the intelligence that acts on it.

## Decision logic

Nova reasons over the four KB articles (seeded by `seed_kb.py`) and applies the
approval tiers from the Refund Approval Policy:

- Duplicate payment ≤ $500 → Finance; > $500 → Finance Manager
- Product return ≤ $1,000 → Support; > $1,000 → Finance
- Anything it cannot verify → manual investigation

The AI analyzes the request, determines eligibility and approval routing, and
recommends the action. A human authorizes the financial action. Money movement
itself is a stub — plug your payment processor into `_finalize_after_approval`
in `webhook_server.py`.

## Project structure

```
src/
  webhook_server.py      Agent orchestration: poller, webhook handler, routing,
                         assess → reply → await approval → finalize
  ai.py                  Amazon Nova (Bedrock Converse): analyze_refund() +
                         write_approval_reply()
  policy.py              Rule-based fallback engine (used if Nova fails)
  mcp_client.py          Freshdesk MCP session, tool calls, KB reader,
                         approval detection, Secrets Manager key loading
  seed_kb.py             One-time: create the 4 refund policy KB articles
  run_once.py            Manually run the agent on one ticket id
  test_connection.py     Check the MCP connection / list tools
infra/
  deploy.ps1             Rebuild the Linux dependency bundle + redeploy the Lambda
  trust-policy.json      Lambda role trust policy
  secret-policy.json     IAM: read the Freshdesk secret
  bedrock-policy.json    IAM: invoke Nova
docs/
  architecture.drawio    Editable architecture diagram (open at diagrams.net)
  ARTICLE.md             Build write-up: problem, approach, learnings, pricing
requirements.txt         Local dev deps
requirements-lambda.txt  Lambda runtime deps
README.md
.env / .env.example      Config
```

## Setup

### 1. Enable Freshdesk MCP and get an API key

Admin → Apps & Integrations → MCP (URL `https://YOUR_DOMAIN.freshdesk.com/mcp`).
API key: Profile Settings → API key.

### 2. Configure

```powershell
pip install -r requirements.txt
Copy-Item .env.example .env   # fill in the values
```

`.env`:
- `FRESHDESK_DOMAIN` — your subdomain (e.g. `jimmathewkochittydineshraj`)
- `FRESHDESK_API_KEY` — API key for local runs (in AWS this comes from Secrets Manager)
- `FRESHDESK_FINANCE_GROUP_ID` — a real group id, or `0` to skip reassignment
- `FRESHDESK_REFUND_POLICY_FOLDER_ID` — the KB folder id from step 3
- `BEDROCK_MODEL_ID` — defaults to `us.amazon.nova-lite-v1:0`

### 3. Seed the knowledge base

```powershell
python src/seed_kb.py
```

Creates the Solution category/folder and 4 articles: Duplicate Payment Refund
Policy, Furniture Returns, Payment Verification, Refund Approval Policy. Put the
printed folder id in `FRESHDESK_REFUND_POLICY_FOLDER_ID`.

### 4. Deploy to AWS

The secret, IAM role, Lambda, API Gateway, and EventBridge rule are already
provisioned. To push code changes:

```powershell
.\infra\deploy.ps1
```

This builds Linux-compatible dependencies (avoiding the Windows `pywin32`
transitive dep and Docker), zips, and updates the Lambda.

## Connect the trigger (Freshdesk automation rules)

Point Freshdesk at the webhook so the agent runs only on real refunds. Webhook URL:

```
https://mv31sm4tpa.execute-api.us-east-1.amazonaws.com/webhooks/freshdesk/refund
```

**Rule A — new refunds** (Admin → Automations → *Ticket Creation* → New rule):
- Condition (match refund intent, not just the word "refund"):
  `Subject` contains `refund` **OR** `charged` **OR** `payment`
  (or trigger on a refund tag/category if your form sets one)
- Action: Trigger Webhook → `POST`, JSON, body `{ "ticket_id": "{{ticket.id}}" }`

**Rule B — approvals** (*Ticket Updates* → New rule):
- Condition: `Note is added`
- Action: same webhook URL and body

Enable both. Leave the EventBridge poller disabled.

## Using it

Email a refund request to your Freshdesk support address. The webhook fires, the
agent reads it, replies to the customer, and posts its reasoning within seconds.
To release the refund, add a private note containing **"Approved"** — Rule B
fires and the agent finalizes.

Run manually against one ticket:

```powershell
python src/run_once.py <ticket_id>
```

## Operations

- **Logs:** CloudWatch `/aws/lambda/freshdesk-refund-agent`
- **Trigger (current):** the EventBridge poller `freshdesk-refund-poller`,
  `rate(5 minutes)`, ENABLED. It scans for refund tickets and processes them.
  If you get a Freshdesk automation rule working, disable the poller with
  `aws events disable-rule --name freshdesk-refund-poller --region us-east-1`
  and rely on the webhook instead (fewer MCP actions).
- **KB caching:** the 4 policy articles are cached in memory per warm Lambda
  container, so the agent does not re-fetch them on every ticket (saves actions).
- **Change poll interval:** `aws events put-rule --name freshdesk-refund-poller
  --schedule-expression "rate(10 minutes)" --region us-east-1` (slower = fewer
  actions).
- **Rotate the API key:** update the secret with
  `aws secretsmanager put-secret-value --secret-id freshdesk/refund-agent
  --secret-string '{"FRESHDESK_API_KEY":"<new-key>"}'`

## Notes / gaps for production

- **Money movement is a stub** — wire your payment processor into
  `_finalize_after_approval`.
- **No webhook signature check** — if you use the API Gateway endpoint publicly,
  add a shared-secret header before trusting payloads.
- **Idempotency:** a ticket tagged `refund-approved` is skipped on repeat runs,
  so it won't double-refund.
- **AI safety:** the agent never approves or moves money; a human "Approved" note
  is always required. Nova failures fall back to the rule engine.
- **Cost:** Nova performs the refund analysis and generates the customer-facing
  responses (fractions of a cent per refund). Freshdesk MCP actions are the real
  constraint — the webhook design spends only a few per refund; the disabled
  poller would spend thousands per day. Mind the monthly action cap.
- Freshdesk's MCP API has no delete tool, so test tickets are removed from the UI.
```
