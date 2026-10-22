"""
Smoke test: launches server.py as an MCP server over stdio, lists its tools,
and calls a few of them the way an agent would.

    python3 test_client.py
"""
import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).parent


def show(title, result, limit=700):
    print(f"\n### {title}")
    text = result.content[0].text if result.content else "(no content)"
    if result.isError:
        print("TOOL ERROR:", text[:limit])
        return
    try:
        text = json.dumps(json.loads(text), indent=1)
    except ValueError:
        pass
    print(text[:limit] + (" ..." if len(text) > limit else ""))


async def main():
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(HERE / "server.py")],
        env=dict(os.environ),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = (await session.list_tools()).tools
            print(f"Server exposes {len(tools)} tools:")
            for t in tools:
                first_line = (t.description or "").strip().splitlines()[0]
                print(f"  - {t.name}: {first_line}")

            show("get_today", await session.call_tool("get_today", {}))
            show("get_company('Goldman')", await session.call_tool("get_company", {"name": "Goldman"}), 900)
            show("find_stale_applications (>=60d, accepted contact)",
                 await session.call_tool("find_stale_applications",
                                         {"min_days": 60, "require_accepted_contact": True, "limit": 2}))
            # Invalid enum value: the SDK should reject this before our code runs.
            show("search_applications(status='pending')  <- invalid on purpose",
                 await session.call_tool("search_applications", {"status": "pending"}))
            # Valid enum, unknown company: our own error dict with suggestions.
            show("get_company('Micro')  <- ambiguous on purpose",
                 await session.call_tool("get_company", {"name": "Micro"}))


if __name__ == "__main__":
    asyncio.run(main())
