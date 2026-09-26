"""
Thin wrapper around the Freshdesk Product MCP server.

Product MCP is tenant-scoped and wraps the public REST v2 API — auth is
just the Freshdesk API key, same permissions as the key's owning agent.
URL shape: https://{domain}.freshdesk.com/mcp (custom domains not supported).
"""

import json
import os
import re
from contextlib import asynccontextmanager

from dotenv import find_dotenv, load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

# Load .env from the project root regardless of where the script is run from
# (code lives in src/, .env lives at the repo root). No-op in Lambda.
load_dotenv(find_dotenv(usecwd=True))


def _load_api_key() -> str:
    """
    Get the Freshdesk API key.

    Prefer AWS Secrets Manager (set FRESHDESK_SECRET_ID to the secret name/ARN,
    e.g. in Lambda). Fall back to the FRESHDESK_API_KEY env var for local runs.
    The secret value is JSON: {"FRESHDESK_API_KEY": "..."}.
    """
    direct = os.environ.get("FRESHDESK_API_KEY")
    if direct:
        return direct

    secret_id = os.environ.get("FRESHDESK_SECRET_ID")
    if secret_id:
        import boto3

        region = os.environ.get("AWS_REGION", "us-east-1")
        client = boto3.client("secretsmanager", region_name=region)
        raw = client.get_secret_value(SecretId=secret_id)["SecretString"]
        try:
            return json.loads(raw)["FRESHDESK_API_KEY"]
        except (json.JSONDecodeError, KeyError):
            return raw  # secret stored as a bare string

    raise RuntimeError("No Freshdesk API key: set FRESHDESK_API_KEY or FRESHDESK_SECRET_ID")


FRESHDESK_DOMAIN = os.environ["FRESHDESK_DOMAIN"]
FRESHDESK_API_KEY = _load_api_key()
MCP_URL = f"https://{FRESHDESK_DOMAIN}.freshdesk.com/mcp"

# Folder that holds the refund policy KB articles (seeded by seed_kb.py).
REFUND_POLICY_FOLDER_ID = int(os.environ.get("FRESHDESK_REFUND_POLICY_FOLDER_ID", "1130000109015"))


@asynccontextmanager
async def freshdesk_mcp_session():
    """Open (and clean up) a session against the Freshdesk MCP server."""
    headers = {"Authorization": FRESHDESK_API_KEY}
    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def call_tool(session: ClientSession, name: str, arguments: dict) -> dict:
    """
    Call one Freshdesk MCP tool (e.g. fetchTicket, updateTicket, replyTicket)
    and return its content parsed as JSON where possible.
    """
    result = await session.call_tool(name, arguments)
    if result.isError:
        raise RuntimeError(f"MCP tool '{name}' failed: {result.content}")

    text_blocks = [block.text for block in result.content if getattr(block, "type", None) == "text"]
    raw = "\n".join(text_blocks)
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}

    # Freshdesk MCP wraps responses in an envelope:
    #   {"status": "OK", "message": "", "data": {...}, "status_code": 200}
    # Unwrap to the inner record so callers get the ticket/group directly.
    if isinstance(parsed, dict) and "data" in parsed and "status_code" in parsed:
        return parsed["data"]
    return parsed


async def start_conversation(session: ClientSession) -> str:
    """
    Some Solution/KB tools require a conversation handle. Open one and return
    its conversation_id (empty string if the server doesn't hand one back).
    """
    conv = await call_tool(session, "start_conversation", {})
    return conv.get("conversation_id") or conv.get("id") or ""


def _strip_html(html: str) -> str:
    """Very light HTML -> text so KB bodies read cleanly in logs/notes."""
    text = re.sub(r"<br\s*/?>", "\n", html or "")
    text = re.sub(r"</(p|li|h[1-6]|ul|ol)>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


# Cache the KB policies across warm Lambda invocations so we don't spend MCP
# actions re-fetching the same 4 articles on every ticket. Refetch only when
# the cache is empty (cold start).
_POLICY_CACHE: list[dict] = []


async def fetch_refund_policies(
    session: ClientSession, folder_id: int | None = None, conv_id: str = ""
) -> list[dict]:
    """
    Read the refund policy KB articles the agent reasons over. Returns a list
    of {"title", "body"} dicts. Cached in memory after the first successful
    load to conserve Freshdesk MCP actions.
    """
    if _POLICY_CACHE:
        return _POLICY_CACHE

    folder_id = folder_id or REFUND_POLICY_FOLDER_ID
    conv_id = conv_id or await start_conversation(session)
    base = {"conversation_id": conv_id, "_reasoning": "Agent loading refund policies as decision context"}

    result = await call_tool(session, "fetchSolutionFolderArticles", {**base, "id": folder_id})
    if isinstance(result, list):
        items = result
    else:
        items = result.get("results") or result.get("articles") or []

    policies = []
    for a in items:
        if isinstance(a, dict):
            policies.append({"title": a.get("title", ""), "body": _strip_html(a.get("description", ""))})
    if policies:
        _POLICY_CACHE.extend(policies)
    return policies


async def fetch_conversations(session: ClientSession, ticket_id: int, conv_id: str = "") -> list[dict]:
    """Return the ticket's notes + replies (newest last) as a list of dicts."""
    conv_id = conv_id or await start_conversation(session)
    base = {"conversation_id": conv_id, "_reasoning": "Agent checking ticket for human approval keyword"}
    result = await call_tool(session, "fetchTicketConversations", {**base, "id": ticket_id})
    if isinstance(result, list):
        return result
    return result.get("results") or result.get("conversations") or []


# A note counts as human approval when an agent (non-incoming) writes a short
# note whose text is the approval keyword — not the agent's own decision
# summary (which merely mentions the word "approve").
# Match "approve"/"approved" plus common typos and synonyms, so a small
# misspelling in a short note doesn't block a refund (e.g. "aprroved",
# "aproved", "approvd", "ok to refund", "authorized", "authorised").
_APPROVAL_RE = re.compile(
    r"\b("
    r"a+p+r*o+v+e*d?"            # approve/approved + typos: aprroved, aproved, approvd
    r"|authoris?z?ed?"           # authorized / authorised
    r"|ok\s*(to\s*)?refund"      # "ok refund" / "ok to refund"
    r"|refund\s*ok"
    r")\b",
    re.IGNORECASE,
)


def find_human_approval(conversations: list[dict]) -> dict | None:
    """
    Return the approving note dict if an agent explicitly approved, else None.

    Guards against the agent's own decision summary and against the customer's
    email by requiring: not incoming, the approval keyword present, and the
    body not being the agent's REFUND AGENT summary.
    """
    for c in conversations:
        body = (c.get("body_text") or c.get("body") or "").strip()
        if not body:
            continue
        if c.get("incoming"):  # customer message, ignore
            continue
        if body.upper().startswith("REFUND AGENT"):  # agent's own summary
            continue
        # Short, deliberate approval note (avoid matching long paragraphs that
        # merely contain the word). Keyword must appear in a concise note.
        if _APPROVAL_RE.search(body) and len(body) <= 120:
            return c
    return None
