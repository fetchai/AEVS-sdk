# AEVS SDK Documentation

> **Beta** — AEVS is in beta. APIs and explorer may change.

**Agent Execution Verification System** — a transparent audit SDK for AI agents by [Fetch.ai](https://fetch.ai).

AEVS intercepts tool calls from your AI agent, builds tamper-evident receipts (HMAC-signed and hash-chained), and sends them to the AEVS backend. No changes to your agent code required.

**Why does this matter?** As AI agents take real-world actions — searching the web, calling APIs, executing code — there is no standard way to prove *what* an agent did, *when*, and in *what order*. AEVS solves this by creating a verifiable audit trail of every tool call your agent makes.

## Who is this for?

- **Agent developers** who want transparent, verifiable records of what their agents do
- **Teams building production agents** that need compliance or audit trails
- **Anyone integrating AI agents** into workflows where trust and accountability matter

## Learning path

### Start here

- **[Getting Started](01-getting-started.md)** — install the SDK, get credentials, and capture your first receipt in under 5 minutes
- **[Core Concepts](02-core-concepts.md)** — understand receipts, hash chains, sessions, and invocation tracking

### Set up your framework (pick one or both)

- **[LangChain & LangGraph](04-langchain-integration.md)** — if you use LangChain tools or LangGraph agents
- **[MCP](05-mcp-integration.md)** — if you use Model Context Protocol tools

### Go deeper

- **[Configuration](03-configuration.md)** — every option explained with defaults, env vars, and production examples
- **[Receipt Verification](06-receipt-verification.md)** — visibility modes, the explorer, and the verify API
- **[Security & Privacy](07-security-and-privacy.md)** — cryptographic design, threat model, and data handling

### Reference

- **[API Reference](08-api-reference.md)** — complete reference for every public function, class, and option
- **[Troubleshooting](09-troubleshooting.md)** — common issues, error messages, and how to fix them

## How it works (at a glance)

```
Your Agent (LangChain / MCP)
  │
  ▼  tool call intercepted automatically
ReceiptBuilder  ──▶  HMAC sign + hash chain
  │
  ▼
Local Buffer (encrypted SQLite)
  │
  ▼  background flush
AEVS Backend  ──▶  verifiable audit trail
```

1. You call `aevs.enable()` — the SDK patches your framework's tool dispatch
2. Every tool call is intercepted, and a signed receipt is created
3. Receipts are buffered locally (encrypted, crash-safe)
4. A background thread flushes receipts to the AEVS backend
5. Anyone can verify a receipt using its `reference_id`

## Quick example

```python
import aevs

aevs.configure(
    api_key="aevs_sk_<key_id>_<hex_secret>",
    agent_id="<your-agent-uuid>",
)
aevs.enable()

# From this point, every tool call is intercepted and recorded.
# No changes to your tools, agents, or LLM setup needed.

# ... run your agent as usual ...

aevs.flush()
aevs.disable()
```

## Supported frameworks

| Framework | Install command | Min version |
|-----------|----------------|-------------|
| LangChain / LangGraph | `pip install aevs[langchain]` | `langchain-core >= 0.2` |
| MCP (Model Context Protocol) | `pip install aevs[mcp]` | `mcp >= 1.20` |

## Get your credentials

1. Go to [aevs.fetch.ai](https://aevs.fetch.ai)
2. Create an account and register your agent
3. Copy your **API key** (`aevs_sk_...`) and **Agent ID** (UUID)

## Links

- [GitHub Repository](https://github.com/fetchai/AEVS-sdk)
- [AEVS Explorer](https://explorer.aevs.fetch.ai) — search receipts by `reference_id` or `receipt_id`
- [Examples](https://github.com/fetchai/AEVS-sdk/tree/main/examples) — runnable scripts to learn from
- [Contributing Guide](https://github.com/fetchai/AEVS-sdk/blob/main/CONTRIBUTING.md)
