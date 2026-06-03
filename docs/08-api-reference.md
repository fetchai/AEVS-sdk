# API Reference

Complete reference for the AEVS SDK public API. Everything is accessed through the `aevs` module.

## Setup

### `aevs.configure(**options)`

Set the SDK configuration. Must be called before `aevs.enable()`.

```python
aevs.configure(
    api_key="aevs_sk_...",
    agent_id="550e8400-...",
    receipt_visibility="private",
)
```

**Parameters:** See [Configuration](03-configuration.md) for the full list.

**Behavior:**
- Validates credentials; logs a warning and enters no-op mode if invalid
- Cannot be called while the SDK is enabled — call `disable()` first
- Falls back to environment variables for `api_key`, `agent_id`, and `receipt_visibility`

### `aevs.reset_config()`

Clear the global configuration. Intended for testing only.

```python
aevs.reset_config()
```

## Lifecycle

### `aevs.enable(frameworks=None)`

Start intercepting tool calls. Patches the detected (or specified) frameworks and starts the background drainer.

```python
aevs.enable()                            # auto-detect frameworks
aevs.enable(frameworks=["langchain"])    # only LangChain
aevs.enable(frameworks=["mcp"])          # only MCP
aevs.enable(frameworks=["langchain", "mcp"])  # both
```

**Parameters:**
- `frameworks` *(list[str], optional)* — which frameworks to patch. If `None`, auto-detects installed frameworks.

**What it does:**
1. Creates the HTTP client, local buffer, and receipt builder
2. Determines session ID (new or crash-recovered)
3. Patches the specified framework adapters
4. Starts the background drainer thread

### `aevs.disable()`

Stop intercepting tool calls. Restores original framework methods, performs a final flush, and shuts down.

```python
aevs.disable()
```

**What it does:**
1. Unpatches all framework adapters
2. Stops the background drainer (with a final flush)
3. Closes the HTTP client and buffer

### `aevs.flush()`

Trigger a synchronous flush of buffered receipts to the backend. Receipts are sent in order; if a receipt fails after retries, the flush stops and remaining receipts stay in the buffer for the next flush cycle.

```python
aevs.flush()
```

Useful before exiting a script or when you need receipts to be submitted. Note that `flush()` returns `None` and does not indicate whether all receipts were successfully sent — some may remain buffered if the backend is temporarily unreachable.

## Health

### `aevs.is_healthy(threshold=3)`

Check if the local buffer is functioning. This only tracks consecutive **buffer write failures** (e.g. full disk, corrupted SQLite file). It does **not** check credentials, backend reachability, or drainer status.

```python
if aevs.is_healthy():
    print("AEVS buffer is working")
else:
    print("AEVS buffer has repeated write failures — check disk space and logs")
```

**Parameters:**
- `threshold` *(int, default 3)* — number of consecutive buffer write failures before reporting unhealthy.

**Returns:** `bool` — `False` after `threshold` consecutive `buffer.store()` failures. Resets to healthy on any successful store.

## Session

### `aevs.get_session_id()`

Get the current session UUID, or `None` if the SDK is not enabled.

```python
aevs.enable()
session = aevs.get_session_id()
# "5db7d195-f84c-4f90-ae12-d74d001d3f9d"

aevs.disable()
aevs.get_session_id()   # None
```

## Reference IDs

### `aevs.get_reference_ids(clear=False)`

Get all tracked reference IDs from the current session.

```python
refs = aevs.get_reference_ids(clear=True)
```

**Parameters:**
- `clear` *(bool, default False)* — if `True`, empties the registry after returning.

**Returns:** List of dicts, each containing:

```python
{
    "seq": 1,
    "tool_name": "search",
    "reference_id": "abc-123-...",
    "run_id": "def-456-...",
    "tool_call_id": "ghi-789-...",
}
```

> **Note:** The `invocation_id` is included in the receipt payload sent to the backend but is not part of the reference registry entries. Use the backend API to filter receipts by `invocation_id`.

### `aevs.get_reference_id(lookup_id)`

Look up the `reference_id` for a given framework `run_id` or `tool_call_id`.

```python
ref_id = aevs.get_reference_id("framework-run-id")
if ref_id:
    print(ref_id)  # "abc-123-..."
```

**Parameters:**
- `lookup_id` *(str)* — the `run_id` or `tool_call_id` to search for.

**Returns:** The `reference_id` string, or `None` if not found.

### `aevs.clear_reference_ids()`

Drop all entries from the reference registry.

```python
aevs.clear_reference_ids()
```

## Exceptions

These are defined in `aevs.exceptions`. Since v0.2.1, the SDK logs warnings instead of raising these to user code. They are documented for completeness.

| Exception | When it occurs |
|-----------|---------------|
| `AEVSError` | Base class for all AEVS exceptions |
| `AEVSConfigError` | Invalid API key format, invalid agent ID |
| `AEVSSerializationError` | Receipt serialization failure |
| `AEVSBufferError` | Local buffer operations failure |
| `AEVSAuthError` | Request signing or authentication failure (HMAC v1 or ECDSA v2) |

## Other exports

| Symbol | Description |
|--------|-------------|
| `aevs.AEVSConfig` | Immutable config dataclass. Rarely used directly — `configure()` creates it. |
| `aevs.__version__` | Installed SDK version string (e.g. `"0.2.1"`). |

## Next steps

- [Getting Started](01-getting-started.md) — see the API in action
- [Configuration](03-configuration.md) — detailed config options
- [Troubleshooting](09-troubleshooting.md) — common issues

---

[< Previous: Security & Privacy](07-security-and-privacy.md) | [Next: Troubleshooting >](09-troubleshooting.md)
