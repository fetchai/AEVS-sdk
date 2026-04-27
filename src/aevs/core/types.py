from __future__ import annotations

from typing import Any, TypedDict


class ReceiptPayload(TypedDict):
    """Typed schema for an AEVS receipt sent to POST /v1/receipts.

    All fields are required. The payload_hmac is added after the initial
    dict is built (it covers all other fields).
    """

    agent_id: str | None
    seq: int
    prev_hash: str
    tool_name: str
    inputs: Any
    output: Any
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
    payload_hmac: str
