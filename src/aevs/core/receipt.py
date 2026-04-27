from __future__ import annotations

import threading
import uuid
from datetime import datetime
from typing import Any

from aevs._version import __version__
from aevs.config import AEVSConfig
from aevs.core.serializer import canonical_json, truncate_field
from aevs.core.types import ReceiptPayload
from aevs.crypto.chain import compute_chain_anchor, compute_receipt_hash
from aevs.crypto.hkdf import derive_key
from aevs.crypto.hmac_auth import compute_hmac


class ReceiptBuilder:
    """Builds tamper-evident receipts for tool calls.

    Maintains sequence counter and hash chain state. Thread-safe.
    One instance per `aevs.enable()` session.
    """

    def __init__(
        self,
        config: AEVSConfig,
        *,
        start_seq: int = 0,
        prev_hash: str | None = None,
    ) -> None:
        self._config = config
        self._seq = start_seq
        self._prev_hash: str | None = prev_hash
        self._lock = threading.Lock()

        self._payload_key = derive_key(config.key_secret, salt="aevs-payload-v1")

    def build(
        self,
        *,
        tool_name: str,
        inputs: Any,
        output: Any,
        status: str,
        error: str | None,
        started_at: datetime,
        ended_at: datetime,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        framework: str = "unknown",
        framework_version: str = "unknown",
        **_extra: Any,
    ) -> ReceiptPayload:
        """Build a receipt dict with payload_hmac and chain fields."""
        cfg = self._config

        inputs, _ = truncate_field(
            inputs,
            cfg.max_payload_bytes,
            float_handling=cfg.float_handling,
            float_precision=cfg.float_precision,
        )
        output, _ = truncate_field(
            output,
            cfg.max_payload_bytes,
            float_handling=cfg.float_handling,
            float_precision=cfg.float_precision,
        )

        with self._lock:
            self._seq += 1
            seq = self._seq

            if self._prev_hash is None:
                prev_hash = compute_chain_anchor(cfg.key_secret)
            else:
                prev_hash = self._prev_hash

            duration_ms = int((ended_at - started_at).total_seconds() * 1000)
            reference_id = str(uuid.uuid4())

            receipt: dict[str, Any] = {
                "reference_id": reference_id,
                "agent_id": cfg.agent_id,
                "seq": seq,
                "prev_hash": prev_hash,
                "tool_name": tool_name,
                "inputs": inputs,
                "output": output,
                "status": status,
                "error": error,
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "duration_ms": duration_ms,
                "run_id": run_id,
                "parent_run_id": parent_run_id,
                "sdk_version": __version__,
                "framework": framework,
                "framework_version": framework_version,
            }

            # Compute payload HMAC over all fields (before adding payload_hmac itself)
            receipt_bytes = canonical_json(
                receipt,
                float_handling=cfg.float_handling,
                float_precision=cfg.float_precision,
            )
            receipt["payload_hmac"] = compute_hmac(self._payload_key, receipt_bytes)

            # Update chain: hash the complete receipt (with payload_hmac) for next prev_hash
            full_bytes = canonical_json(
                receipt,
                float_handling=cfg.float_handling,
                float_precision=cfg.float_precision,
            )
            self._prev_hash = compute_receipt_hash(full_bytes)

            return receipt  # type: ignore[return-value]

    @property
    def seq(self) -> int:
        return self._seq
