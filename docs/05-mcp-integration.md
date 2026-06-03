# MCP Integration

> **Prerequisite:** [Getting Started](01-getting-started.md)

AEVS supports the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) by intercepting tool calls made through MCP client sessions.

## Install

```bash
pip install aevs[mcp]
```

Requires `mcp >= 1.20`.

## How it works

The SDK patches `ClientSession.call_tool` from the `mcp` package. Every time your agent calls an MCP tool, the SDK:

1. Captures the tool name and arguments
2. Runs the original `call_tool`
3. Serializes the result
4. Builds a signed receipt
5. Stores it in the local buffer

MCP is async-only, so the SDK patches the async path.

## Basic example

Using an MCP server over stdio (e.g. a local server script):

```python
import asyncio
import aevs
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    aevs.configure(api_key="aevs_sk_...", agent_id="...")
    aevs.enable(frameworks=["mcp"])

    server_params = StdioServerParameters(
        command="python",
        args=["my_mcp_server.py"],
    )

    async with stdio_client(server_params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()

            # This call is intercepted — a receipt is created
            result = await session.call_tool("search", arguments={"query": "AI news"})
            print(result)

    refs = aevs.get_reference_ids(clear=True)
    for ref in refs:
        print(f"Tool: {ref['tool_name']}, Ref: {ref['reference_id']}")

    aevs.flush()
    aevs.disable()

asyncio.run(main())
```

> **Note:** Replace `"my_mcp_server.py"` with the path to your MCP server. The `stdio_client` helper comes from the `mcp` package and manages the subprocess transport. For SSE-based servers, use `mcp.client.sse.sse_client` instead.

## Result serialization

MCP tools can return different types of content. The SDK handles each type:

| Content type | How it is recorded |
|-------------|-------------------|
| `structuredContent` (JSON) | Stored as `{"structured": <object>}` |
| Single text block | Flattened to a plain string |
| Multiple text blocks | Stored as `{"content": [{"type": "text", "text": "..."}]}` |
| Binary content (images, audio) | **Not stored raw** — replaced with a SHA-256 hash and byte count |
| Resource content | Stored as `{"type": "resource", "uri": "..."}` (URI only) |
| Resource link | Stored as `{"type": "resource_link", "uri": "..."}` |

Binary content is never included in receipts directly. Instead, the receipt records a hash fingerprint (`_aevs_data_sha256` and `_aevs_data_bytes`) so you can verify the content existed without transmitting large files.

## Experimental MCP Tasks API

If an MCP tool returns a `CreateTaskResult` (from the experimental MCP tasks API), the SDK **skips receipt creation** for that call and logs a warning. Task results represent long-running task handles rather than completed tool outputs, so they are not yet supported in the receipt pipeline.

## Passing metadata

You can pass optional `run_id` and `parent_run_id` through the `meta` keyword argument if your application tracks these:

```python
result = await session.call_tool(
    "search",
    arguments={"query": "AI news"},
    meta={"run_id": "my-run-123", "parent_run_id": "parent-456"},
)
```

These values are included in the receipt for correlation with your own tracking systems.

## Error handling

If an MCP tool returns an error (`isError=True` in the result), the receipt records:

- `status`: `"error"`
- `error`: the text content from the error result

Your agent continues normally — the SDK never raises exceptions from receipt processing.

## Runnable examples

See the [examples directory](../examples/) for runnable scripts. The [01_local_quickstart.py](../examples/01_local_quickstart.py) script demonstrates AEVS with a LangChain tool and can be adapted for MCP workflows.

## Using MCP with LangChain (langchain-mcp-adapters)

If you use `langchain-mcp-adapters` to bridge MCP tools into LangChain, **both adapters might be active**. The SDK handles this automatically:

- A tracking flag prevents the same tool call from being recorded twice
- The SDK logs a one-time warning when both adapters are active alongside the bridge library

You do not need to do anything special — just enable AEVS normally and both frameworks work together.

## Next steps

- [LangChain Integration](04-langchain-integration.md) — if you also use LangChain
- [Receipt Verification](06-receipt-verification.md) — verify your receipts
- [Core Concepts](02-core-concepts.md) — understand sessions and hash chains

---

[< Previous: LangChain Integration](04-langchain-integration.md) | [Next: Receipt Verification >](06-receipt-verification.md)
