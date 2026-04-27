# AEVS SDK

Agent Execution Verification System — transparent audit SDK for AI agents.

Intercepts tool calls from supported frameworks, builds tamper-evident receipts (HMAC-signed, hash-chained), and sends them to the AEVS backend. Zero changes to your agent code.

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

aevs.configure(api_key="aevs_sk_<key_id>_<hex_secret>")
aevs.enable()

# Every tool call from this point is intercepted.
# No changes to tools, agents, or LLM setup.
```

## API

```python
aevs.configure(api_key=..., **options)   # Set configuration (required before enable)
aevs.enable()                            # Auto-detect frameworks and start intercepting
aevs.enable(frameworks=["langchain"])    # Or specify explicitly
aevs.disable()                           # Unpatch all frameworks, restore originals
aevs.flush()                             # Force-send buffered receipts to backend
```

### Configuration Options

| Parameter | Default | Description |
|-----------|---------|-------------|
| `api_key` | *(required)* | SDK key from customer creation (`aevs_sk_<id>_<hex>`) |
| `agent_id` | `None` | Agent UUID to tag receipts with |
| `base_url` | `https://aevs.fetch.ai/v1` | AEVS backend URL |
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
# [{"seq": 1, "tool_name": "search", "reference_id": "abc-...", "run_id": "def-..."}, ...]
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

## Development

### Prerequisites

- Python 3.10+
- [Poetry](https://python-poetry.org/)

### Setup

```bash
git clone https://github.com/fetchai/AEVS-sdk.git && cd AEVS-sdk
poetry install --all-extras   # installs langchain + mcp extras for dev
```

### Running Tests

```bash
poetry run pytest              # all tests
poetry run pytest -v           # verbose
poetry run pytest --cov=aevs   # with coverage report
```

### Running a Specific Test File

```bash
poetry run pytest tests/test_integration.py -v
```

### Linting

```bash
poetry run ruff check src/ tests/
poetry run ruff check --fix src/ tests/   # auto-fix
```

### Type Checking

```bash
poetry run mypy src/
```

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
