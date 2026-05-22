# Getting Started

Get AEVS running with your agent in under 5 minutes.

## Prerequisites

- Python 3.10 or higher
- An AEVS account at [aevs.fetch.ai](https://aevs.fetch.ai)

## Step 1: Install

Install with the LangChain extra (used in the examples below):

```bash
pip install aevs[langchain]
```

Other extras you can add depending on your framework:

```bash
pip install aevs[mcp]         # Model Context Protocol
pip install aevs[langchain]   # LangChain / LangGraph (included above)
```

If you only need the core SDK without framework integrations:

```bash
pip install aevs
```

## Step 2: Get your credentials

1. Sign up at [aevs.fetch.ai](https://aevs.fetch.ai)
2. Register your agent on the dashboard
3. You will get two things:
   - **API Key** — looks like `aevs_sk_myKeyId_a1b2c3d4e5f6...` (the part after the second underscore is a hex secret, at least 32 characters)
   - **Agent ID** — a UUID like `550e8400-e29b-41d4-a716-446655440000`

## Step 3: Configure and enable

You can pass credentials directly:

```python
import aevs

aevs.configure(
    api_key="aevs_sk_myKeyId_a1b2c3d4...",
    agent_id="550e8400-e29b-41d4-a716-446655440000",
)
aevs.enable()
```

Or use environment variables (recommended for production):

```bash
export AEVS_API_KEY="aevs_sk_myKeyId_a1b2c3d4..."
export AEVS_AGENT_ID="550e8400-e29b-41d4-a716-446655440000"
```

```python
import aevs

aevs.configure()   # picks up from env vars
aevs.enable()
```

## Step 4: Run your agent

Once enabled, every tool call is intercepted automatically. Here is a minimal example using a LangChain tool:

```python
import aevs
from langchain_core.tools import tool

@tool
def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b

# Configure and start AEVS
aevs.configure(
    api_key="aevs_sk_myKeyId_a1b2c3d4...",
    agent_id="550e8400-e29b-41d4-a716-446655440000",
)
aevs.enable(frameworks=["langchain"])

# This tool call is now intercepted — a receipt is created
result = add.invoke({"a": 2, "b": 3})
print(result)  # 5

# Get the receipt reference
refs = aevs.get_reference_ids(clear=True)
print(refs)
# [{"seq": 1, "tool_name": "add", "reference_id": "abc-123-...", ...}]

# Flush and shut down
aevs.flush()
aevs.disable()
```

## Step 5: Verify your receipt

Every receipt gets a `reference_id`. You can verify it through:

**The AEVS Explorer** — go to [explorer.aevs.fetch.ai](https://explorer.aevs.fetch.ai) and search by `reference_id` or `receipt_id` to find the receipt details.

**The API** (no auth required):
```
GET https://api.aevs.fetch.ai/v1/receipts/verify/<reference_id>
```

**Note:** Receipts only appear on the public explorer if the agent's dashboard visibility is enabled. See [Receipt Verification](receipt-verification.md) for details.

## What happens if credentials are missing?

The SDK never crashes your agent. If credentials are missing or invalid:

- A warning is logged
- The SDK runs in **no-op mode** — your agent works normally, receipts just are not recorded
- Call `aevs.is_healthy()` to check if the local buffer is working (it tracks consecutive buffer write failures, not credential or backend status)

## Next steps

- [Core Concepts](core-concepts.md) — understand receipts, hash chains, and sessions
- [Configuration](configuration.md) — explore all configuration options
- [LangChain Integration](langchain-integration.md) — detailed LangChain/LangGraph guide
- [MCP Integration](mcp-integration.md) — detailed MCP guide
