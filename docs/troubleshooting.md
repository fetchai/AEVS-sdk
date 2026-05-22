# Troubleshooting

Common issues, log messages, and how to fix them.

## The SDK is not recording receipts

**Symptoms:** No reference IDs, `get_reference_ids()` returns an empty list.

**Check these in order:**

1. **Did you call `configure()` and `enable()`?**

```python
aevs.configure(api_key="aevs_sk_...", agent_id="...")
aevs.enable()  # must be called before any tool calls
```

2. **Are credentials valid?** Check logs for warnings like:
   - `"AEVS: Invalid API key format. Expected: aevs_sk_<key_id>_<hex_secret> AEVS will run in no-op mode — no receipts will be captured."` — your key must match `aevs_sk_<key_id>_<hex_secret>` with the hex secret being at least 32 characters
   - `"AEVS: agent_id must be a valid UUID, got '...' AEVS will run in no-op mode — no receipts will be captured."` — must be a UUID with dashes like `550e8400-e29b-41d4-a716-446655440000`

3. **Is the framework detected?** If auto-detection fails, specify explicitly:

```python
aevs.enable(frameworks=["langchain"])
```

4. **Check health:**

```python
print(aevs.is_healthy())  # False means the buffer has repeated write failures
```

## "AEVS: invalid api_key format"

Your API key must follow this exact format:

```
aevs_sk_<key_id>_<hex_secret>
```

- `aevs_sk_` — fixed prefix
- `<key_id>` — alphanumeric identifier
- `<hex_secret>` — at least 32 hexadecimal characters (0-9, a-f)

Example: `aevs_sk_myKey123_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4`

Get a valid key at [aevs.fetch.ai](https://aevs.fetch.ai).

## "AEVS: invalid agent_id"

The agent ID must be a canonical UUID with dashes:

```
550e8400-e29b-41d4-a716-446655440000   ✓ correct
550e8400e29b41d4a716446655440000       ✗ missing dashes
agent_550e8400-e29b-41d4-a716-...      ✗ has a prefix
```

## "Cannot reconfigure while enabled"

You tried to call `aevs.configure()` while the SDK is already enabled. Disable first:

```python
aevs.disable()
aevs.configure(api_key=..., agent_id=...)
aevs.enable()
```

## Receipts are not reaching the backend

**Symptoms:** Receipts are created locally but not appearing in the Explorer.

1. **Check network:** Can your machine reach `https://api.aevs.fetch.ai`?

2. **Force a flush:**

```python
aevs.flush()  # sends buffered receipts in order; stops on first persistent failure
```

3. **Check the drain interval:** By default, receipts are flushed every 5 seconds. If your script exits before the first flush, call `aevs.flush()` before `aevs.disable()`.

4. **Check logs:** The drainer logs warnings on HTTP failures. Look for messages like `"AEVS: drain failed, next retry in <N>s (<N> consecutive failures)"`.

## Both LangChain and MCP adapters are active

If you use `langchain-mcp-adapters` (a bridge library), you may see:

```
AEVS: both 'mcp' and 'langchain' adapters are active and langchain-mcp-adapters is installed.
Tool calls through langchain-mcp-adapters will be intercepted by the outer adapter only
(no double-counting) thanks to the _aevs_tracking_active guard.
```

This is a one-time warning, not an error. The SDK handles this automatically — a `_aevs_tracking_active` context variable prevents the same tool call from being recorded twice.

## Large tool outputs are truncated

If a tool returns a large response, the SDK truncates it based on `max_payload_bytes` (default 1 MB). The receipt will contain:

```json
{"_truncated": true, "_original_bytes": 2500000, "_preview": "first 500 chars of the value..."}
```

To increase the limit:

```python
aevs.configure(max_payload_bytes=5_000_000)  # 5 MB
```

Or to reduce it for bandwidth-sensitive environments:

```python
aevs.configure(max_payload_bytes=100_000)  # 100 KB
```

## Buffer is full

When the buffer reaches `max_buffer_records` (default 10,000), the oldest pending receipt is evicted and a gap marker is inserted. This is auditable — the chain records that a receipt was dropped.

**Solutions:**
- Increase the limit: `aevs.configure(max_buffer_records=50_000)`
- Flush more often: `aevs.configure(drain_interval_ms=2000)`
- Check if the backend is reachable (receipts pile up when it is not)

## NaN or Infinity values in tool output

If a tool returns `NaN` or `Infinity` (not valid JSON), the SDK replaces them with `null` and logs a warning. The receipt is still created.

To be strict about this:

```python
aevs.configure(float_handling="raise")
```

This still converts the values (receipts are still created) but logs a more prominent warning.

## Process crashed — are my receipts lost?

No. The local buffer is on disk and crash-safe. When you restart and call `aevs.enable()`:

- The SDK detects unflushed receipts
- It reuses the previous session ID (crash recovery)
- The hash chain continues where it left off
- You will see a log: `"AEVS: mid-session crash recovery — resuming session_id=..."`

## Next steps

- [Configuration](configuration.md) — adjust settings to fix common issues
- [Security & Privacy](security-and-privacy.md) — understand the security model
- [API Reference](api-reference.md) — full function reference
