# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Batch receipt sending** — the background drainer now sends buffered receipts in batches (up to `max_batch_size`, default 50) via `POST /v1/receipts/batch` instead of one-by-one. Reduces HTTP round-trips and improves throughput for high-volume agents. Configurable via `aevs.configure(max_batch_size=...)`. The SDK auto-detects backend support: if the batch endpoint returns 404/405 the drainer permanently falls back to single-receipt sending for the rest of the session.
- **ECDSA v2 signing** — new `aevs_sk2_` API keys use ECDSA P-256 / SHA-256 asymmetric signatures for both receipt payload signing and HTTP request authentication. The private key stays on the SDK host; the backend stores only the SPKI public key. Existing HMAC (`aevs_sk_`) keys continue to work; the SDK auto-detects the key type.
- **Invocation ID tracking** — each `graph.invoke()` / `.ainvoke()` / `.stream()` / `.astream()` call on a LangGraph compiled graph automatically gets a unique `invocation_id` (UUID v4). All tool calls within that graph execution share the same ID in their receipts, regardless of how many steps they span. Subgraphs inherit the parent's ID. Direct tool calls outside a graph get `None`. Falls back to LangSmith `trace_id` when available.
- **`proof_only` payload hashes** — `proof_only` receipts now include `input_hash` and `output_hash` fields containing SHA-256 hashes of the actual pre-redaction payloads. Verifiers can confirm distinct inputs/outputs without accessing the raw data.

## [0.2.1] - 2026-05-15

### Changed
- **Breaking:** Default `receipt_visibility` changed from `"public"` to `"private"`.
- The SDK never raises `AEVSConfigError` or `AEVSSerializationError` to user code. All errors are logged as warnings and the SDK degrades gracefully (no-op mode or auto-corrected defaults).
- `configure()`: invalid API key or agent ID now logs a warning and enters no-op mode instead of raising.
- `configure()`: invalid non-critical config fields (e.g. `float_precision=-1`) are auto-corrected to defaults with a warning instead of raising.
- `enable()`: adapter loading failures (unknown adapter, import error, framework not installed) are logged as warnings and skipped instead of raising.
- `enable()`: buffer/client init failures log a warning and enter no-op mode instead of raising.
- `get_config()` returns `None` instead of raising when the SDK is not configured.
- Serializer: `NaN`/`inf` floats are replaced with `null` (with a warning) instead of raising `AEVSSerializationError`.
- Serializer: `float_handling="raise"` mode now falls through to decimal-string conversion with a warning instead of raising.

### Added
- `receipt_visibility` configuration parameter (`"public"`, `"private"`, `"proof_only"`). In `proof_only` mode, tool inputs and outputs are stripped from receipts — only the cryptographic proof of the call is recorded. Also settable via `AEVS_RECEIPT_VISIBILITY` env var.

## [0.2.0] - 2026-05-12

### Changed
- License changed from MIT to Apache License 2.0; `pyproject.toml` `license` field and PyPI classifier updated to match.
- **Breaking:** `agent_id` is now a required string on `aevs.configure()` (or via `AEVS_AGENT_ID`). Missing credentials log a warning and the SDK enters no-op mode instead of crashing.
- `agent_id` is validated as a canonical UUID, with diagnostics for dashless hex, prefixed identifiers, and non-canonical forms.
- **Breaking:** LangChain receipts now store the tool's argument dict in `inputs` instead of the full `ToolCall` envelope. `id` / `name` / `type` are still captured as `tool_call_id` / `tool_name` / `framework`.

### Added
- Developer Certificate of Origin (DCO) v1.1: contributions require `git commit -s`, enforced by a GitHub Actions check. See [`DCO`](https://github.com/fetchai/AEVS-sdk/blob/main/DCO) and [`CONTRIBUTING.md`](https://github.com/fetchai/AEVS-sdk/blob/main/CONTRIBUTING.md).
- `examples/` directory with three runnable walkthroughs: local quickstart, OpenAI + LangChain, and Fetch.ai ASI:One. See [`examples/README.md`](https://github.com/fetchai/AEVS-sdk/blob/main/examples/README.md).
- `tool_call_id` field on `get_reference_ids()` entries; `get_reference_id(lookup_id)` now resolves by either `run_id` or `tool_call_id`.

### Fixed
- `enable()` clean-drain branch now deletes the persisted `chain_state` row before minting a new `session_id`, so a crash between drain and first store no longer mis-routes the next session into mid-session recovery.

### Documentation
- README session-lifecycle section documents crash-recovery session reuse and lists the two INFO log lines (`mid-session crash recovery — resuming session_id=…` / `clean drain detected — minting new session …`) that signal which path fired.

## [0.1.0] - 2026-05-07

Initial public release.

### Added
- LangChain and MCP adapter support — intercept tool calls without modifying agent code.
- Encrypted local SQLite buffer with hash-chained, tamper-evident receipts (HMAC + HKDF-derived keys).
- Background drainer with exponential backoff sends buffered receipts to the AEVS backend.
- Reference ID registry correlates tool calls with receipts; lookup by framework `run_id` or `tool_call_id`.
- Per-session UUID — every `enable()` mints a fresh `session_id`, stamps it on each receipt, and anchors the hash chain to it. Two SDK processes that share an API key cannot fork the chain.
- Public API surface: `aevs.configure`, `enable`, `disable`, `flush`, `is_healthy`, `get_session_id`, `get_reference_id`, `get_reference_ids`, `clear_reference_ids`, `reset_config`, plus `AEVSConfig` and the `AEVSError` family of exceptions.
- Resilience: `enable()` recovers from a corrupt or non-SQLite `buffer_path` by purging and recreating the buffer; the hash chain survives full drains via a persisted `chain_state` row; `AEVSClient.close()` / `aclose()` dispatch async cleanup on the loop that owns the connection pool.

### Security
- HMAC-signed receipts over canonical JSON; HKDF derives per-purpose keys from the SDK secret.
- Receipt session anchoring prevents chain-forking attacks where a compromised agent could fabricate an alternate history under the same `(key_id, agent_id)`.
- Default `base_url` is HTTPS (`https://api.aevs.fetch.ai/v1`); the SDK warns when a non-loopback `http://` URL is configured.
- HTTP error response bodies are truncated in WARNING logs; the full slice is kept at DEBUG only.

[Unreleased]: https://github.com/fetchai/AEVS-sdk/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/fetchai/AEVS-sdk/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/fetchai/AEVS-sdk/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/fetchai/AEVS-sdk/releases/tag/v0.1.0
