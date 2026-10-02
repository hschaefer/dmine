# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 dmine contributors
"""Smoke test: connect to the dmine MCP server over stdio and call tools."""
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "dmine.mcp_server"],
        cwd=str(Path(__file__).resolve().parent),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print("TOOLS:", [t.name for t in tools.tools])
            r1 = await session.call_tool("status", {"channel": "100000000000000001"})
            print("STATUS:", json.dumps(json.loads(r1.content[0].text), indent=1)[:400])
            r2 = await session.call_tool("recent", {"channel": "100000000000000001", "limit": 2, "include_embeds": False})
            data = json.loads(r2.content[0].text)
            print("RECENT:", len(data["messages"]), "messages; first:", json.dumps(data["messages"][0], ensure_ascii=False)[:220])
            r3 = await session.call_tool("search", {"query": "Palladium", "limit": 2})
            print("SEARCH:", len(json.loads(r3.content[0].text)["hits"]), "hits")


asyncio.run(main())
