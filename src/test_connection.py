"""
Quick connectivity check against the real Freshdesk Product MCP server.

Run: python test_connection.py

It opens a session, lists the tools the server exposes, and (optionally)
fetches one ticket so you can confirm auth + custom-field names before
wiring up the live webhook.
"""

import asyncio
import os
import sys

from mcp_client import MCP_URL, call_tool, freshdesk_mcp_session


async def main() -> None:
    print(f"Connecting to {MCP_URL} ...")
    async with freshdesk_mcp_session() as session:
        tools = await session.list_tools()
        print(f"\nConnected. Server exposes {len(tools.tools)} tools:")
        for t in tools.tools:
            print(f"  - {t.name}: {(t.description or '').splitlines()[0][:80]}")

        # If a ticket id is passed on the command line, fetch it so you can
        # inspect its custom_fields (to set cf_refund_amount / cf_order_age_days).
        if len(sys.argv) > 1:
            ticket_id = int(sys.argv[1])
            print(f"\nFetching ticket {ticket_id} ...")
            ticket = await call_tool(session, "fetchTicket", {"id": ticket_id})
            print("custom_fields:", ticket.get("custom_fields"))
            print("priority:", ticket.get("priority"), "status:", ticket.get("status"))


if __name__ == "__main__":
    asyncio.run(main())
