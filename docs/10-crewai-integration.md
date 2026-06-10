# CrewAI Integration

> **Prerequisite:** [Getting Started](01-getting-started.md)

AEVS supports [CrewAI](https://www.crewai.com/) by intercepting tool calls on both of CrewAI's execution paths — native function-calling and the text-based ReAct fallback.

## Install

```bash
pip install aevs[crewai]
```

Requires `crewai >= 1.0`.

## How it works

CrewAI dispatches tool calls through two paths depending on LLM capability:

| Path | When it's used | What AEVS patches |
|------|---------------|-------------------|
| **Native function-calling** | LLM supports function calling (default for OpenAI, Anthropic, Gemini) | `BaseTool.run`, `Tool.run` |
| **Text ReAct fallback** | LLM does not support function calling, or `function_calling_llm` is set | `CrewStructuredTool.invoke`, `CrewStructuredTool.ainvoke` |

The SDK detects which path is active at runtime and intercepts accordingly. You do not need to configure anything — just enable AEVS.

## Basic example

```python
import aevs
from crewai import Agent, Crew, Task
from crewai.tools import tool

@tool("search")
def search(query: str) -> str:
    """Search the web for information."""
    return f"Results for: {query}"

aevs.configure(api_key="aevs_sk_...", agent_id="...")
aevs.enable(frameworks=["crewai"])

agent = Agent(
    role="Researcher",
    goal="Find information",
    backstory="You are a research assistant.",
    tools=[search],
)
task = Task(
    description="Search for recent AI news.",
    agent=agent,
    expected_output="A summary of AI news.",
)
crew = Crew(agents=[agent], tasks=[task])
result = crew.kickoff()

refs = aevs.get_reference_ids(clear=True)
for ref in refs:
    print(f"Tool: {ref['tool_name']}, Ref: {ref['reference_id']}")

aevs.flush()
aevs.disable()
```

## Invocation grouping

When you run a crew via `Crew.kickoff()` or `Crew.akickoff()`, AEVS sets an `invocation_id` that groups all tool calls within that run. This is recorded in every receipt and visible in the explorer.

Derived methods like `kickoff_async`, `kickoff_for_each`, and `akickoff_for_each` all delegate to these two entry points, so grouping works automatically.

Tool calls made outside a crew run (e.g. calling `tool.run()` directly) will have a `null` invocation_id.

## Tool types covered

| Tool style | Covered |
|-----------|---------|
| `BaseTool` subclass with `_run` | Yes |
| `@tool` decorator | Yes |
| `CrewStructuredTool.from_function` | Yes |
| Async tools (`async def _run` / async functions) | Yes |

## Error handling

If a tool raises an exception, the receipt records:

- `status`: `"error"`
- `error`: the exception message as a string

The exception is re-raised to CrewAI as usual — the SDK never swallows errors.

If the AEVS handler itself fails (e.g. network issue, serialization problem), the tool execution is unaffected. AEVS logs a debug-level message and continues.

## Cross-adapter deduplication

If you enable multiple adapters (e.g. `crewai` + `langchain`), a tracking flag prevents the same tool call from being recorded twice. The first adapter to intercept a call claims it; subsequent adapters forward without creating a receipt.

## Next steps

- [LangChain Integration](04-langchain-integration.md) — if you also use LangChain
- [MCP Integration](05-mcp-integration.md) — if you also use MCP tools
- [Receipt Verification](06-receipt-verification.md) — verify your receipts
- [Core Concepts](02-core-concepts.md) — understand sessions and hash chains

---

[< Previous: Troubleshooting](09-troubleshooting.md) | [Back to index](README.md)
