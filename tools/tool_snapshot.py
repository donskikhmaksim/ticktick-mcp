"""Снимок реестра команд. Печатает в stdout канонический JSON."""
import asyncio, json
from ticktick_mcp.src import server

async def main():
    tools = await server.mcp.list_tools()
    dump = [
        {"name": t.name,
         "description": t.description,
         "inputSchema": t.inputSchema,
         "annotations": getattr(t, "annotations", None) and t.annotations.model_dump()}
        for t in sorted(tools, key=lambda x: x.name)
    ]
    print(json.dumps(dump, ensure_ascii=False, indent=2, sort_keys=True))

asyncio.run(main())
