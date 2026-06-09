<p align="center">
  <img src="https://raw.githubusercontent.com/fetchai/AEVS-sdk/main/assets/logo.svg" alt="AEVS SDK" width="200">
</p>

<h1 align="center">AEVS SDK</h1>

<p align="center">
  <strong>Agent Execution Verification System — transparent audit SDK for AI agents</strong>
</p>

<p align="center">
  <a href="https://pypi.org/project/aevs/"><img src="https://img.shields.io/pypi/v/aevs?color=blue" alt="PyPI"></a>
  <a href="https://pypi.org/project/aevs/"><img src="https://img.shields.io/pypi/pyversions/aevs" alt="Python"></a>
  <a href="https://github.com/fetchai/AEVS-sdk/blob/main/LICENSE"><img src="https://img.shields.io/github/license/fetchai/AEVS-sdk" alt="License"></a>
</p>

<p align="center">
  <a href="https://github.com/fetchai/AEVS-sdk/blob/main/docs/README.md">Documentation</a> &middot;
  <a href="https://explorer.aevs.fetch.ai">Explorer</a> &middot;
  <a href="https://github.com/fetchai/AEVS-sdk/tree/main/examples">Examples</a> &middot;
  <a href="https://aevs.fetch.ai">Get Credentials</a>
</p>

---

Intercepts tool calls from supported frameworks, builds tamper-evident receipts (HMAC-signed, hash-chained), and sends them to the AEVS backend. Zero changes to your agent code.

## Installation

```bash
pip install aevs
```

With framework extras:

```bash
pip install aevs[langchain]   # LangChain / LangGraph
pip install aevs[mcp]         # Model Context Protocol
```

| Framework | Extra | Min version |
|-----------|-------|-------------|
| LangChain / LangGraph | `aevs[langchain]` | `langchain-core >= 0.2` |
| MCP | `aevs[mcp]` | `mcp >= 1.20` |

## Quick Start

```python
import aevs
from langchain_core.tools import tool

@tool
def search(query: str) -> str:
    """Search the web."""
    return f"Results for: {query}"

aevs.configure(
    api_key="aevs_sk_<key_id>_<hex_secret>",
    agent_id="<your-agent-uuid>",
)
aevs.enable()

result = search.invoke({"query": "AI news"})

refs = aevs.get_reference_ids(clear=True)
print(refs)
# [{"seq": 1, "tool_name": "search", "reference_id": "abc-123-...", ...}]

aevs.flush()
aevs.disable()
```

Credentials can also be set via `AEVS_API_KEY` / `AEVS_AGENT_ID` environment variables. If missing, the SDK logs a warning and runs in no-op mode — your agent keeps working, receipts just aren't recorded.

## How It Works

```
Agent (LangChain / MCP)
  │
  ▼  tool call intercepted
ReceiptBuilder  ──▶  HMAC sign + hash chain
  │
  ▼
LocalBuffer (SQLite, encrypted at rest)
  │
  ▼  background drainer
AEVSClient  ──▶  POST /v1/receipts  ──▶  AEVS Backend
```

1. `aevs.enable()` patches your framework's tool dispatch
2. Every tool call is intercepted and a signed receipt is created
3. Receipts are buffered locally (encrypted, crash-safe)
4. A background thread flushes receipts to the AEVS backend
5. Verify any receipt using its `reference_id`

## API Overview

```python
aevs.configure(api_key=..., **options)   # set configuration
aevs.enable()                            # start intercepting tool calls
aevs.disable()                           # stop and restore originals
aevs.flush()                             # send buffered receipts now
aevs.get_session_id()                    # current session UUID
aevs.get_reference_ids(clear=True)       # all captured reference IDs
aevs.get_reference_id(tool_call_id)      # lookup single reference ID
aevs.is_healthy()                        # buffer write health check
```

See the [full API reference](https://github.com/fetchai/AEVS-sdk/blob/main/docs/08-api-reference.md) for details.

## Receipt Visibility

Control what data is included in each receipt:

| Mode | Inputs & outputs | Use case |
|------|-----------------|----------|
| `"public"` | Included | Full audit — verifiers can inspect everything |
| `"private"` | Included | Signed and submitted, but restricted access (default) |
| `"proof_only"` | Stripped | Prove a tool call happened without revealing data |

```python
aevs.configure(api_key=..., agent_id=..., receipt_visibility="proof_only")
```

## Examples

| Script | What it teaches | Requirements |
|--------|----------------|--------------|
| [`01_local_quickstart.py`](https://github.com/fetchai/AEVS-sdk/blob/main/examples/01_local_quickstart.py) | Minimal SDK loop — invoke a tool, see AEVS capture it | AEVS credentials only |
| [`02_openai_agent.py`](https://github.com/fetchai/AEVS-sdk/blob/main/examples/02_openai_agent.py) | LangChain agent with OpenAI | `OPENAI_API_KEY` + AEVS |
| [`03_asi_agent.py`](https://github.com/fetchai/AEVS-sdk/blob/main/examples/03_asi_agent.py) | Same agent with [ASI:One](https://asi1.ai) — provider-agnostic | `ASI_API_KEY` + AEVS |

See [`examples/README.md`](https://github.com/fetchai/AEVS-sdk/blob/main/examples/README.md) for setup instructions.

## Documentation

| Page | Description |
|------|-------------|
| [Getting Started](https://github.com/fetchai/AEVS-sdk/blob/main/docs/01-getting-started.md) | Install, configure, and capture your first receipt |
| [Core Concepts](https://github.com/fetchai/AEVS-sdk/blob/main/docs/02-core-concepts.md) | Receipts, hash chains, sessions, invocation tracking |
| [Configuration](https://github.com/fetchai/AEVS-sdk/blob/main/docs/03-configuration.md) | All configuration options with defaults |
| [LangChain Integration](https://github.com/fetchai/AEVS-sdk/blob/main/docs/04-langchain-integration.md) | LangChain / LangGraph guide |
| [MCP Integration](https://github.com/fetchai/AEVS-sdk/blob/main/docs/05-mcp-integration.md) | Model Context Protocol guide |
| [Receipt Verification](https://github.com/fetchai/AEVS-sdk/blob/main/docs/06-receipt-verification.md) | Visibility modes and verification |
| [Security & Privacy](https://github.com/fetchai/AEVS-sdk/blob/main/docs/07-security-and-privacy.md) | Threat model and data handling |
| [API Reference](https://github.com/fetchai/AEVS-sdk/blob/main/docs/08-api-reference.md) | Complete function reference |
| [Troubleshooting](https://github.com/fetchai/AEVS-sdk/blob/main/docs/09-troubleshooting.md) | Common issues and fixes |

## Data & Privacy

- Receipts are buffered locally in an encrypted SQLite database
- Submitted to the AEVS backend over HTTPS
- Use `receipt_visibility="proof_only"` to prevent inputs/outputs from leaving the host
- AEVS is **tamper-evident**, not tamper-proof — it detects modification after the fact

## Development

```bash
git clone https://github.com/fetchai/AEVS-sdk.git && cd AEVS-sdk
make install        # poetry install --all-extras
make check          # lint + typecheck + tests (the CI gate)
```

```bash
make test           # run tests
make test-cov       # tests with coverage
make lint           # ruff check
make format         # ruff format + auto-fix
make typecheck      # mypy --strict
make build          # build sdist + wheel
```

## Contributing

See [CONTRIBUTING.md](https://github.com/fetchai/AEVS-sdk/blob/main/CONTRIBUTING.md) for the full guide.

## Security

Please **do not** open a public issue for security problems. See [SECURITY.md](https://github.com/fetchai/AEVS-sdk/blob/main/SECURITY.md) for the disclosure process.

## License

[Apache 2.0](https://github.com/fetchai/AEVS-sdk/blob/main/LICENSE)
