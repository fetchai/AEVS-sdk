from __future__ import annotations

from typing import Any, TypedDict


class ReceiptPayload(TypedDict):
    """Typed schema for an AEVS receipt sent to POST /v1/receipts.

    All fields are required. The payload_hmac is added after the initial
    dict is built (it covers all other fields, including session_id, so
    tampering with the session boundary is detectable).
    """

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
    reference_id: str
    sdk_version: str
    framework: str
    framework_version: str
    receipt_visibility: str
    payload_hmac: str
