# Core Concepts

This page explains the key ideas behind AEVS. Understanding these will help you make the most of the SDK.

## Receipts

A **receipt** is a signed record of a single tool call. Every time your agent calls a tool (search, calculator, API, etc.), the SDK creates a receipt containing:

| Field | What it records |
|-------|----------------|
| `tool_name` | Name of the tool that was called |
| `inputs` | What was passed to the tool |
| `output` | What the tool returned |
| `status` | `"success"` or `"error"` |
| `error` | Error message (only when `status` is `"error"`) |
| `started_at` / `ended_at` | Timestamps (ISO 8601, UTC) |
| `duration_ms` | How long the call took |
| `reference_id` | Unique ID to verify this receipt later |
| `session_id` | UUID for the current SDK session |
| `invocation_id` | Groups tool calls within a single graph execution (LangGraph) |
| `seq` | Sequence number within the session |
| `prev_hash` | Hash of the previous receipt (forms the chain) |
| `run_id` / `parent_run_id` | Framework-assigned correlation IDs |
| `sdk_version` | Version of the AEVS SDK that created the receipt |
| `framework` / `framework_version` | Which framework was intercepted (e.g. `"langchain"`, `"mcp"`) |
| `receipt_visibility` | Visibility mode set at creation time |
| `payload_hmac` | Cryptographic signature proving the receipt was not tampered with |

Receipts are the atomic unit of AEVS. One tool call = one receipt.

## Hash Chains

Receipts are not independent — they are **hash-chained**. Each receipt includes the hash of the previous receipt in its `prev_hash` field.

```
Receipt #1          Receipt #2          Receipt #3
┌──────────┐       ┌──────────┐       ┌──────────┐
│ seq: 1   │       │ seq: 2   │       │ seq: 3   │
│ prev: ■──┼──┐    │ prev: ■──┼──┐    │ prev: ■──┼──...
│ hmac: X  │  │    │ hmac: Y  │  │    │ hmac: Z  │
└──────────┘  │    └──────────┘  │    └──────────┘
              │         ▲        │         ▲
              └─────────┘        └─────────┘
         hash(receipt #1)   hash(receipt #2)
```

**Why does this matter?**

- If someone deletes receipt #2, the chain breaks — receipt #3's `prev_hash` will not match anything
- If someone modifies receipt #2, its hash changes — receipt #3's `prev_hash` will not match
- This makes the audit trail **tamper-evident**: you can detect any modification or deletion after the fact

The very first receipt's `prev_hash` is a **chain anchor** — a value derived from your API key and session ID, so the chain is rooted in your identity.

### Chain status

The backend assigns a `chain_status` to each receipt when it is ingested:

| Status | Meaning |
|--------|---------|
| `anchor` | First receipt in the session — chain starts here |
| `linked` | `seq` is previous + 1 and `prev_hash` matches the hash of the prior receipt |
| `mismatch` | `prev_hash` does not match the expected value |
| `gap` | `seq` skipped one or more numbers (e.g. buffer eviction) |
| `broken` | Chain integrity could not be verified |
| `unverified` | Chain verification was not performed (e.g. missing session context) |

You can see chain status on the explorer or in the verification API response.

## Sessions

A **session** starts when you call `aevs.enable()` and ends when you call `aevs.disable()`. Each session gets a unique UUID.

```python
aevs.enable()
session = aevs.get_session_id()
# "5db7d195-f84c-4f90-ae12-d74d001d3f9d"

# ... all receipts in this session share this session_id ...

aevs.disable()
aevs.get_session_id()  # None
```

Sessions help you group receipts:
- Filter receipts by session to see everything from one SDK run
- Each session has its own hash chain

**Crash recovery**: If your process crashes with unflushed receipts, the next `enable()` reuses the same session ID. This keeps old and new receipts in one continuous chain. A clean shutdown always starts a fresh session.

## Invocation IDs

When using LangGraph agents, a single `graph.invoke()` call might trigger multiple tool calls across several steps. The SDK groups them with an **invocation ID** — a UUID v4 that is shared by all tool calls within one graph execution.

```
session_id:      |<------------ entire session ------------>|
invocation_id:   |<-- invoke 1 -->|    |<-- invoke 2 -->|
tool calls:      | t1 | t2 | t3  |    | t4 | t5 |
```

- All tools within one `graph.invoke()` / `.ainvoke()` / `.stream()` / `.astream()` share the same `invocation_id`
- Subgraphs inherit the parent's invocation ID
- Separate `graph.invoke()` calls get different IDs
- Direct `tool.invoke()` calls (no graph) have `invocation_id = None`
- When LangSmith tracing is active, the `trace_id` is used as a fallback invocation ID for tools called outside a compiled graph

This is fully automatic — no code changes needed. The SDK patches `CompiledStateGraph` entry points when `langgraph` is installed.

## Reference IDs vs Receipt IDs

Each receipt has two identifiers — it is important to understand the difference:

| | `reference_id` | `receipt_id` |
|---|---|---|
| **Generated by** | The SDK (client-side) | The AEVS backend (server-side) |
| **When available** | Immediately, before the receipt is even submitted | After the receipt is submitted and stored on the backend |
| **Accessible in code** | Yes — via `aevs.get_reference_ids()` | No — only visible on the explorer or API response |
| **Format** | UUID v4 | Internal backend identifier |

The `reference_id` is what you work with in your code. It is generated at the moment the receipt is created and is available right away:

```python
refs = aevs.get_reference_ids(clear=True)
# [{"seq": 1, "tool_name": "search", "reference_id": "abc-123-...", ...}]
```

The `receipt_id` is assigned by the AEVS backend when the receipt is stored. You will see it on the [AEVS Explorer](https://explorer.aevs.fetch.ai) or in the API verification response.

Both can be used to search for receipts on the explorer.

## Local Buffer

Receipts are not sent to the backend immediately. They go through a **local buffer** first:

1. Receipt is created and encrypted
2. Stored in a local SQLite database (`~/.aevs/buffer.db`)
3. A background thread flushes them to the AEVS backend every few seconds

This design means:
- **Your agent is never blocked** waiting for the backend
- **Receipts survive crashes** — the buffer is on disk
- **Network outages are fine** — receipts queue up and flush when connectivity returns

## No-op Mode

If something goes wrong during setup (missing credentials, invalid config, or buffer/client initialization failure), the SDK switches to **no-op mode**. Your agent keeps running normally — receipts just are not recorded. This is by design: AEVS should never break your agent.

> **Note:** A backend outage does **not** trigger no-op mode. Receipts are queued in the local buffer and flushed automatically when connectivity returns. No-op mode only activates when the SDK cannot initialize at all.

Check health programmatically:

```python
if not aevs.is_healthy():
    print("AEVS buffer has repeated write failures — check disk space and logs")
```

## Next steps

- [Configuration](configuration.md) — tune buffer size, flush intervals, and more
- [Receipt Verification](receipt-verification.md) — visibility modes and how to verify receipts
- [Security & Privacy](security-and-privacy.md) — how data is protected
