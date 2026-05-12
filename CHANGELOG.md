# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

_No changes yet._

## [0.2.0] - 2026-05-12

### Changed
- License changed from MIT to Apache License 2.0; `pyproject.toml` `license` field and PyPI classifier updated to match.
- **Breaking:** `agent_id` is now a required string on `aevs.configure()` (or via `AEVS_AGENT_ID`). Missing credentials log a warning and the SDK enters no-op mode instead of crashing.
- `agent_id` is validated as a canonical UUID, with diagnostics for dashless hex, prefixed identifiers, and non-canonical forms.
- **Breaking:** LangChain receipts now store the tool's argument dict in `inputs` instead of the full `ToolCall` envelope. `id` / `name` / `type` are still captured as `tool_call_id` / `tool_name` / `framework`.

### Added
- Developer Certificate of Origin (DCO) v1.1: contributions require `git commit -s`, enforced by a GitHub Actions check. See [`DCO`](DCO) and [`CONTRIBUTING.md`](CONTRIBUTING.md).
- `examples/` directory with three runnable walkthroughs: local quickstart, OpenAI + LangChain, and Fetch.ai ASI:One. See [`examples/README.md`](examples/README.md).
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

[Unreleased]: https://github.com/fetchai/AEVS-sdk/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/fetchai/AEVS-sdk/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/fetchai/AEVS-sdk/releases/tag/v0.1.0
