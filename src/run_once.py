"""
Run the refund agent once against a real ticket, end-to-end, without the
webhook. Lets you watch the agent read, decide, and act on a live ticket.

Run: python run_once.py <ticket_id>
"""

import asyncio
import sys

from webhook_server import process_refund_ticket


async def main() -> None:
    ticket_id = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    await process_refund_ticket(ticket_id)


if __name__ == "__main__":
    asyncio.run(main())
