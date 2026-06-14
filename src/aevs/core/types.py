from __future__ import annotations

from typing import Any, TypedDict


class _ReceiptRequired(TypedDict):
    """Fields present on every AEVS receipt regardless of visibility mode."""

    agent_id: str
    session_id: str
    seq: int
    prev_hash: str
    tool_name: str
    inputs: Any | None
    output: Any | None
    status: str
    error: str | None
    started_at: str
    ended_at: str
    duration_ms: int
    run_id: str | None
    parent_run_id: str | None
    invocation_id: str | None
    reference_id: str
    sdk_version: str
    framework: str
    framework_version: str
    receipt_visibility: str
    payload_hmac: str


class ReceiptPayload(_ReceiptRequired, total=False):
    """Typed schema for an AEVS receipt sent to POST /v1/receipts.

    The fields inherited from :class:`_ReceiptRequired` are always present. The
    ``payload_hmac`` is added after the initial dict is built (it covers all
    other fields, including session_id, so tampering with the session boundary
    is detectable).

    ``input_hash`` / ``output_hash`` are present **only** for ``proof_only``
    receipts: in that mode ``inputs`` / ``output`` are redacted to ``None`` and
    replaced by these SHA-256 digests (see ``ReceiptBuilder.build``). They are
    therefore optional in the schema (``total=False``).
    """

    input_hash: str
    output_hash: str
