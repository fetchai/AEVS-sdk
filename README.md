# AEVS SDK

Agent Execution Verification System — transparent audit SDK for AI agents.

Intercepts tool calls from supported frameworks, builds tamper-evident receipts (HMAC-signed, hash-chained), and sends them to the AEVS backend. Zero changes to your agent code.

## Compatibility

- Python 3.10+
- Framework adapters:
  - `aevs[langchain]` for LangChain / LangGraph tool interception
  - `aevs[mcp]` for MCP tool interception (requires `mcp>=1.20`)

## Installation

```bash
pip install aevs
```

With framework extras:

```bash
pip install aevs[langchain]   # LangChain / LangGraph support
pip install aevs[mcp]         # MCP tool support
```

## Quick Start

```python
import aevs

aevs.configure(
    api_key="aevs_sk_<key_id>_<hex_secret>",
    agent_id="<your-agent-uuid>",
)
aevs.enable()

# Every tool call from this point is intercepted.
# No changes to tools, agents, or LLM setup.
```

Both `api_key` and `agent_id` can also be set via environment variables
(`AEVS_API_KEY` / `AEVS_AGENT_ID`). If either is missing the SDK logs a
warning and runs in no-op mode — your agent keeps working, receipts are
just not recorded. Get your credentials at https://aevs.fetch.ai.

## Examples

Three runnable scripts live in [`examples/`](https://github.com/fetchai/AEVS-sdk/tree/main/examples):

| Script | Teaches | Needs |
|--------|---------|-------|
| [`01_local_quickstart.py`](https://github.com/fetchai/AEVS-sdk/blob/main/examples/01_local_quickstart.py) | The minimal SDK loop — invoke a tool, see AEVS capture it | `AEVS_API_KEY`, `AEVS_AGENT_ID`. No LLM. |
| [`02_openai_agent.py`](https://github.com/fetchai/AEVS-sdk/blob/main/examples/02_openai_agent.py) | A LangChain agent with OpenAI; AEVS records each tool call the model picks | `OPENAI_API_KEY` + AEVS credentials |
| [`03_asi_agent.py`](https://github.com/fetchai/AEVS-sdk/blob/main/examples/03_asi_agent.py) | The same agent rewired to Fetch.ai's [ASI:One](https://asi1.ai) — proves AEVS is provider-agnostic | `ASI_API_KEY` + AEVS credentials |

See [`examples/README.md`](https://github.com/fetchai/AEVS-sdk/blob/main/examples/README.md) for the recommended order and setup.

## API

```python
aevs.configure(api_key=..., **options)   # Set configuration (required before enable)
aevs.enable()                            # Auto-detect frameworks and start intercepting
aevs.enable(frameworks=["langchain"])    # Or specify explicitly
aevs.disable()                           # Unpatch all frameworks, restore originals
aevs.flush()                             # Force-send buffered receipts to backend
aevs.get_session_id()                    # UUID minted at enable(); stamped on every receipt
aevs.is_healthy()                        # False after sustained buffer write failures
```

### Configuration Options

| Parameter | Default | Description |
|-----------|---------|-------------|
| `api_key` | **required** — falls back to `AEVS_API_KEY` env var | SDK key (`aevs_sk_<id>_<hex>`) — get one at https://aevs.fetch.ai |
| `agent_id` | **required** — falls back to `AEVS_AGENT_ID` env var | Agent UUID from the AEVS dashboard |
| `base_url` | `https://api.aevs.fetch.ai/v1` | AEVS backend URL |
| `signing_timeout_ms` | `2000` | HTTP timeout for receipt submission |
| `float_handling` | `"decimal_string"` | How floats are serialized (`decimal_string` or `raise`) |
| `float_precision` | `6` | Decimal places for float serialization |
| `max_payload_bytes` | `1048576` | Max receipt payload size (1 MB) |
| `buffer_path` | `~/.aevs/buffer.db` | SQLite buffer file path |
| `max_buffer_records` | `10000` | Max buffered receipts before eviction |
| `drain_interval_ms` | `5000` | Background flush interval |
| `max_reference_entries` | `1000` | Reference ID registry capacity |

### Reference IDs

Every intercepted tool call gets a `reference_id` (UUID v4) embedded in its receipt. The SDK keeps these in a bounded FIFO registry.

```python
response = await agent.ainvoke({"messages": [("user", query)]})
refs = aevs.get_reference_ids(clear=True)
# [{"seq": 1, "tool_name": "search", "reference_id": "abc-...", "run_id": "def-...", "tool_call_id": "ghi-..."}, ...]
```

Verify any `reference_id` via the public backend endpoint (no auth required):

```
GET /v1/receipts/verify/{reference_id}
```

### Reference ID Registry

```python
aevs.get_reference_id(run_id)       # Lookup by framework run_id
aevs.get_reference_ids(clear=True)  # Get all entries, then clear
aevs.clear_reference_ids()          # Drop all entries
```

### Session IDs

Each `enable()` mints a fresh UUIDv4 **session id** — *or recovers a
persisted one if the previous run crashed with unflushed receipts*. The
id is stamped on every receipt produced in that session and participates
in the hash chain anchor; two SDK processes that share an API key cannot
fork the chain by construction.

```python
aevs.enable()
session = aevs.get_session_id()
# "5db7d195-f84c-4f90-ae12-d74d001d3f9d"

aevs.disable()
aevs.get_session_id()
# None
```

**Crash recovery semantics.** If the previous process exited with
un-flushed receipts (network down, OS kill, hard crash), the next
`enable()` reuses that session's id so old and new receipts ship as one
hash-linked chain — you'll see the same id across runs in this case, by
design. A clean shutdown (all receipts flushed) always mints a fresh
id. The INFO log line on each `enable()` —
`AEVS: mid-session crash recovery — resuming session_id=...` vs.
`AEVS: clean drain detected — minting new session ...` — tells you
which path fired.

Useful for log correlation: every receipt carries `session_id`, so
filtering receipts by session in the AEVS backend isolates a single
SDK run.

## Data & privacy

AEVS receipts may include **tool inputs and outputs**, which can contain secrets or PII depending
on what your tools return (e.g. prompts, retrieved documents, API responses).

- Receipts are buffered locally in an encrypted SQLite database (default `~/.aevs/buffer.db`).
- Receipts are submitted to the AEVS backend over HTTPS by default (`base_url`).

You are responsible for ensuring your tool layer does not emit sensitive data you cannot store or
transmit. If needed, redact at the tool boundary (before data reaches the agent runtime).

## Threat model / non-goals

- AEVS is **tamper-evident**, not tamper-proof. It helps detect modification/reordering of receipts
  after the fact; it does not secure a fully compromised host process.
- The SDK signs requests with a key derived from your API key secret; protect the API key like any
  other credential.

## Development

### Prerequisites

- Python 3.10+
- [Poetry](https://python-poetry.org/)

### Setup

```bash
git clone https://github.com/fetchai/AEVS-sdk.git && cd AEVS-sdk
make install   # poetry install --all-extras
```

### Common commands

The `Makefile` wraps the everyday workflow. Run `make help` to see the
full list.

```bash
make test        # run the test suite
make test-cov    # tests with coverage (HTML report in ./htmlcov)
make lint        # ruff check
make format      # ruff format + auto-fix
make typecheck   # mypy --strict on src/
make check       # lint + typecheck + tests (the CI gate)
make build       # build sdist + wheel into ./dist
```

You can still call the underlying tools directly:

```bash
poetry run pytest tests/test_integration.py -v
poetry run ruff check src/ tests/
poetry run mypy src/
```

## Contributing

See [CONTRIBUTING.md](https://github.com/fetchai/AEVS-sdk/blob/main/CONTRIBUTING.md) for the full guide — branch
naming, Conventional Commits, the PR checklist, and release flow.

## Reporting Security Issues

Please **do not** open a public GitHub issue for security problems.
See [SECURITY.md](https://github.com/fetchai/AEVS-sdk/blob/main/SECURITY.md) for the disclosure process.

## Architecture

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

- **Interception**: Framework-specific patches capture tool inputs/outputs
- **Signing**: Each receipt is HMAC-signed (HKDF-derived keys) with a hash chain linking sequential calls
- **Buffering**: Receipts are encrypted and stored locally in SQLite, flushed in the background
- **Resilience**: Buffer survives process restarts; flush retries on transient failures

## Project Structure

```
aevs-sdk/
├── src/aevs/
│   ├── __init__.py        Public API (configure, enable, disable, flush)
│   ├── _api.py            Core state management, enable/disable, flush
│   ├── _drainer.py        Background flush of buffered receipts
│   ├── _version.py        Package version
│   ├── config.py          Configuration dataclass + validation
│   ├── exceptions.py      SDK exception types
│   ├── adapters/          Framework-specific interceptors
│   │   ├── base.py        Base adapter interface
│   │   ├── langchain.py   LangChain / LangGraph interceptor
│   │   └── mcp.py         MCP tool interceptor
│   ├── core/
│   │   ├── buffer.py      Encrypted SQLite local buffer
│   │   ├── client.py      HTTP client (sync + async)
│   │   ├── receipt.py     ReceiptBuilder — HMAC, hash chain
│   │   ├── serializer.py  Canonical JSON serialization
│   │   ├── signer.py      Request signing (HKDF + HMAC)
│   │   └── types.py       Typed payload shapes
│   └── crypto/
│       ├── chain.py       Hash chain helpers
│       ├── hkdf.py        HKDF key derivation
│       └── hmac_auth.py   HMAC authentication
├── tests/                 Test suite
├── pyproject.toml         Poetry packaging + tool config
├── poetry.lock
├── LICENSE
└── README.md
```
