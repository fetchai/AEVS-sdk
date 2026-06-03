# LangChain & LangGraph Integration

AEVS integrates with LangChain and LangGraph by patching tool dispatch at the framework level. Your tools, agents, and LLM setup stay exactly the same.

## Install

```bash
pip install aevs[langchain]
```

Requires `langchain-core >= 0.2`.

## How it works

When you call `aevs.enable()`, the SDK patches two methods on LangChain's `BaseTool` class:

- `BaseTool.invoke` (sync)
- `BaseTool.ainvoke` (async)

Every tool that inherits from `BaseTool` — including `@tool`-decorated functions — is automatically intercepted. The SDK:

1. Records the start time
2. Runs the original tool call
3. Captures the result (or error)
4. Builds a signed receipt with tool name, inputs, output, timing, and metadata
5. Stores it in the local buffer

When you call `aevs.disable()`, the original methods are restored.

## Basic example

```python
import aevs
from langchain_core.tools import tool

@tool
def search(query: str) -> str:
    """Search the web."""
    return f"Results for: {query}"

@tool
def add(a: int, b: int) -> str:
    """Add two numbers."""
    return str(a + b)

aevs.configure(api_key="aevs_sk_...", agent_id="...")
aevs.enable()

# Both calls produce receipts
search.invoke({"query": "weather today"})
add.invoke({"a": 2, "b": 2})

refs = aevs.get_reference_ids(clear=True)
for ref in refs:
    print(f"{ref['tool_name']}: {ref['reference_id']}")

aevs.flush()
aevs.disable()
```

## With a LangGraph agent

Here is a full example with an OpenAI-powered agent:

```python
import asyncio
import aevs
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent
from langchain_core.tools import tool

@tool
def get_weather(city: str) -> str:
    """Get weather for a city."""
    return f"Sunny, 25°C in {city}"

@tool
def get_time(timezone: str) -> str:
    """Get current time in a timezone."""
    return f"14:30 in {timezone}"

async def main():
    aevs.configure(
        api_key="aevs_sk_...",
        agent_id="...",
        receipt_visibility="public",
    )
    aevs.enable()

    llm = ChatOpenAI(model="gpt-4o-mini")
    agent = create_agent(llm, [get_weather, get_time])

    response = await agent.ainvoke({
        "messages": [("user", "What's the weather and time in London?")]
    })

    # The agent may call both tools — each gets a receipt
    refs = aevs.get_reference_ids(clear=True)
    for ref in refs:
        print(f"Tool: {ref['tool_name']}, Ref: {ref['reference_id']}")

    aevs.flush()
    aevs.disable()

asyncio.run(main())
```

## Invocation tracking with LangGraph

When using a compiled LangGraph (`CompiledStateGraph` from `StateGraph` or `create_agent`), the SDK automatically tracks **invocation IDs**. All tool calls within a single graph execution share the same `invocation_id` in their receipts.

```python
# First invoke — tools here will share invocation_id "aaa-..."
await agent.ainvoke({"messages": [("user", "question 1")]})

# Second invoke — tools here will share a different invocation_id "bbb-..."
await agent.ainvoke({"messages": [("user", "question 2")]})
```

The SDK patches `CompiledStateGraph.invoke`, `.ainvoke`, `.stream`, and `.astream`. Each top-level call gets a new UUID v4 invocation ID that is propagated to all tool calls within that execution via a `ContextVar`.

| Scenario | `invocation_id` |
|----------|----------------|
| Tools inside a LangGraph agent | Shared UUID for that invoke |
| Subgraphs | Inherits parent's ID |
| `graph.batch([...])` | Each item gets its own ID |
| Direct `tool.invoke()` (no graph) | `None` |
| Separate `graph.invoke()` calls | Different UUIDs |

You can filter receipts by `invocation_id` on the backend to see all tool calls from a single graph execution.

## Looking up receipts by tool call

If you are processing `ToolMessage` objects from the agent response, you can look up the receipt for each specific tool call:

```python
from langchain_core.messages import ToolMessage

response = await agent.ainvoke({"messages": [("user", query)]})

for msg in response["messages"]:
    if isinstance(msg, ToolMessage):
        ref_id = aevs.get_reference_id(msg.tool_call_id)
        if ref_id:
            print(f"Tool: {msg.name}")
            print(f"Reference ID: {ref_id}")
```

## Input normalization

LangChain sometimes wraps tool inputs in a `ToolCall` envelope that includes extra metadata (name, id, type). The SDK strips this and records only the actual arguments dictionary. This keeps receipts clean and consistent regardless of how the tool was invoked.

## LangSmith compatibility

If you use LangSmith for tracing, AEVS works alongside it. When `langsmith` is installed and tracing is active, the SDK can use LangSmith's `trace_id` as a fallback invocation ID for tools called outside a compiled graph but inside a traced context.

## Next steps

- [MCP Integration](mcp-integration.md) — if you also use MCP tools
- [Receipt Verification](receipt-verification.md) — verify your receipts
- [API Reference](api-reference.md) — full API details
