# Security & Privacy

AEVS is designed to be a transparent audit layer. This page covers how data is protected, what the SDK can and cannot guarantee, and how to control privacy.

## Cryptographic design

### HMAC signatures

Every receipt is signed using HMAC-SHA256. The signing key is derived from your API key secret using HKDF (HMAC-based Key Derivation Function) with a purpose-specific salt.

```
API key secret
     │
     ▼  HKDF-SHA256
Derived key (purpose: "aevs-payload-v1")
     │
     ▼  HMAC-SHA256
payload_hmac field in receipt
```

Different operations use different derived keys (different HKDF salts), so a key used for payload signing cannot be reused for request authentication or buffer encryption.

| Purpose | HKDF salt |
|---------|-----------|
| Receipt payload signing | `aevs-payload-v1` |
| HTTP request authentication | `aevs-request-v1` |
| Buffer encryption | `aevs-encrypt-v1` |
| Chain anchor | `aevs-chain-v1\|{session_id}` |

### Hash chains

Each receipt includes the SHA-256 hash of the previous receipt. This creates a chain where any modification or deletion is detectable after the fact. See [Core Concepts](core-concepts.md) for details.

### Request signing

When the SDK sends receipts to the backend, each HTTP request is signed:

```
Signature = HMAC-SHA256(
    key = derived_key("aevs-request-v1"),
    message = "{ISO timestamp}\n{SHA256(request body)}"
)
```

Headers sent: `X-AEVS-Key-Id`, `X-AEVS-Timestamp`, `X-AEVS-Signature`.

## Data at rest

The local buffer (`~/.aevs/buffer.db`) is a SQLite database encrypted with AES-256-GCM. The encryption key is derived from your API key secret via HKDF.

- Each receipt is encrypted individually before storage
- The buffer uses SQLite WAL mode for crash safety
- Chain state (last sequence number, last hash, session ID) is persisted separately so the chain can resume after a crash

### Key rotation detection

The buffer stores a fingerprint of the API key used. If you change your API key, the buffer detects the mismatch and resets the chain state. This prevents mixing receipts signed with different keys.

## Data in transit

All communication with the AEVS backend uses HTTPS by default. The SDK warns if you configure a non-loopback `http://` URL.

## What data is in receipts?

By default (`receipt_visibility="private"`), receipts include:

- **Tool name** — the name of the tool function
- **Inputs** — what was passed to the tool (could include user prompts, queries, etc.)
- **Output** — what the tool returned (could include API responses, documents, etc.)
- **Metadata** — timing, sequence number, IDs, status

**Important:** Tool inputs and outputs can contain sensitive data depending on what your tools do. For example:

- A search tool might receive user queries
- A database tool might return personal records
- An API tool might process financial data

## Controlling data exposure

### Use `proof_only` mode

If you cannot have tool data leaving the host, use `proof_only`:

```python
aevs.configure(
    api_key=..., agent_id=...,
    receipt_visibility="proof_only",
)
```

Inputs and outputs are stripped (set to `null`) before the receipt is submitted. Only metadata (tool name, timing, status), signatures, and chain data are stored. No one can retrieve the payloads, not even the owner.

### Redact at the tool boundary

For more granular control, sanitize data before it reaches the agent runtime:

```python
@tool
def search_database(query: str) -> str:
    """Search with redacted results."""
    results = db.query(query)
    return redact_pii(results)  # clean before returning
```

### Limit payload size

Large tool outputs are automatically truncated:

```python
aevs.configure(max_payload_bytes=100_000)  # 100 KB limit
```

Truncated fields are replaced with a marker: `{"_truncated": true, ...}`.

## Threat model

### What AEVS provides

- **Tamper evidence** — any modification to a receipt or its position in the chain is detectable
- **Authenticity** — receipts are signed with your key, proving they came from your SDK instance
- **Ordering proof** — the hash chain proves the sequence of tool calls

### What AEVS does not provide

- **Tamper prevention** — a fully compromised host process could skip creating receipts entirely. AEVS detects tampering after the fact; it does not prevent it.
- **Tool action verification** — a receipt proves the SDK recorded a tool call and its result. It does not independently verify that the tool actually performed the action (e.g., actually sent an email).

### Key security

Your API key secret is used for all cryptographic operations. Treat it like any other credential:

- Do not commit it to version control
- Use environment variables in production
- Rotate it if compromised (the buffer will detect the change)

## Next steps

- [Receipt Verification](receipt-verification.md) — how to verify receipts
- [Configuration](configuration.md) — all privacy-related settings
- [Troubleshooting](troubleshooting.md) — common issues
