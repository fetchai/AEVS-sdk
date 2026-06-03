# Configuration

All configuration is done through `aevs.configure()`. Call it before `aevs.enable()`.

## Basic setup

```python
import aevs

aevs.configure(
    api_key="aevs_sk_myKeyId_a1b2c3d4...",
    agent_id="550e8400-e29b-41d4-a716-446655440000",
)
aevs.enable()
```

## Environment variables

Instead of passing values directly, you can use environment variables:

| Env variable | Corresponds to |
|-------------|----------------|
| `AEVS_API_KEY` | `api_key` |
| `AEVS_AGENT_ID` | `agent_id` |
| `AEVS_RECEIPT_VISIBILITY` | `receipt_visibility` |

If both are set, the explicit parameter wins.

```bash
export AEVS_API_KEY="aevs_sk_myKeyId_a1b2c3d4..."
export AEVS_AGENT_ID="550e8400-e29b-41d4-a716-446655440000"
```

```python
import aevs
aevs.configure()   # reads from env
aevs.enable()
```

## All options

### Credentials

| Parameter | Default | Description |
|-----------|---------|-------------|
| `api_key` | *required* | Your SDK key in the format `aevs_sk_<key_id>_<hex_secret>`. Get one at [aevs.fetch.ai](https://aevs.fetch.ai). Falls back to `AEVS_API_KEY` env var. |
| `agent_id` | *required* | Your agent's UUID from the AEVS dashboard. Must be a canonical UUID with dashes. Falls back to `AEVS_AGENT_ID` env var. |

### Backend

| Parameter | Default | Description |
|-----------|---------|-------------|
| `base_url` | `https://api.aevs.fetch.ai/v1` | AEVS backend URL. Change this only if you run a self-hosted backend. |
| `signing_timeout_ms` | `2000` | HTTP timeout for receipt submission (milliseconds). |

### Privacy

| Parameter | Default | Description |
|-----------|---------|-------------|
| `receipt_visibility` | `"private"` | Controls what data is in receipts. See [Receipt Verification](receipt-verification.md) for details. Options: `"public"`, `"private"`, `"proof_only"`. |

### Serialization

| Parameter | Default | Description |
|-----------|---------|-------------|
| `float_handling` | `"decimal_string"` | How floats are serialized. `"decimal_string"` converts them to strings like `"3.140000"`. `"raise"` also converts but logs a warning. |
| `float_precision` | `6` | Number of decimal places when serializing floats. |
| `max_payload_bytes` | `1048576` | Maximum receipt payload size (1 MB). Inputs/outputs exceeding this are truncated with a marker. |

### Buffer

| Parameter | Default | Description |
|-----------|---------|-------------|
| `buffer_path` | `~/.aevs/buffer.db` | Path to the SQLite buffer file. |
| `max_buffer_records` | `10000` | Maximum receipts stored in the buffer. When full, the oldest pending receipt is evicted and a gap marker is inserted. |
| `drain_interval_ms` | `5000` | How often (in ms) the background thread flushes receipts to the backend. |

### Reference tracking

| Parameter | Default | Description |
|-----------|---------|-------------|
| `max_reference_entries` | `1000` | How many reference IDs to keep in memory. Uses a FIFO queue — oldest entries are dropped when full. |

## Example: production config

```python
import os
import aevs

aevs.configure(
    api_key=os.environ["AEVS_API_KEY"],
    agent_id=os.environ["AEVS_AGENT_ID"],
    receipt_visibility="private",
    buffer_path="./buffer.db",
    max_payload_bytes=512_000,       # 500 KB limit
    drain_interval_ms=3000,          # flush every 3 seconds
    max_buffer_records=50_000,       # larger buffer for high-volume agents
)
```

## Example: local development

```python
aevs.configure(
    api_key="aevs_sk_devKey_aabbccdd...",
    agent_id="550e8400-e29b-41d4-a716-446655440000",
    receipt_visibility="public",       # full visibility for debugging
    buffer_path="./local_buffer.db",   # buffer in project dir
    drain_interval_ms=1000,            # flush frequently
)
```

## Validation rules

The SDK validates your config and handles mistakes gracefully:

- **API key format**: Must match `aevs_sk_<key_id>_<hex_secret>` (HMAC v1) or `aevs_sk2_<key_id>_<hex_secret>` (ECDSA v2) where the secret is at least 32 hex characters. Invalid key → no-op mode. The SDK auto-detects the auth version from the key prefix.
- **Agent ID format**: Must be a canonical UUID with dashes (e.g., `550e8400-e29b-41d4-a716-446655440000`). A dashless UUID or one with a prefix will produce a helpful error message.
- **Non-critical fields**: Invalid values (like negative `drain_interval_ms`) are auto-corrected to defaults with a warning.
- **Reconfiguring while enabled**: Not allowed. Call `aevs.disable()` first.
- **HTTP base URLs**: Non-loopback `http://` URLs trigger a security warning (use `https://`).

## Next steps

- [Receipt Verification](receipt-verification.md) — understand `receipt_visibility` modes in detail
- [Security & Privacy](security-and-privacy.md) — how your data is protected
