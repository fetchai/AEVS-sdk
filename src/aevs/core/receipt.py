from __future__ import annotations

import hashlib
import threading
import uuid
from datetime import datetime
from typing import Any, cast

from cryptography.hazmat.primitives.asymmetric import ec

from aevs._version import __version__
from aevs.config import AEVSConfig
from aevs.core.serializer import canonical_json, truncate_field
from aevs.core.types import ReceiptPayload
from aevs.crypto.chain import compute_chain_anchor, compute_receipt_hash
from aevs.crypto.ecdsa import _private_key_from_hex, ecdsa_sign_payload_v2
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
        session_id: str,
        start_seq: int = 0,
        prev_hash: str | None = None,
    ) -> None:
        self._config = config
        self._session_id = session_id
        self._seq = start_seq
        self._prev_hash: str | None = prev_hash
        self._lock = threading.Lock()

        self._payload_key: bytes | None = None
        self._ecdsa_private_key: ec.EllipticCurvePrivateKey | None = None

        if config.auth_version == 2:
            self._ecdsa_private_key = _private_key_from_hex(config.key_secret.hex())
        else:
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
        invocation_id: str | None = None,
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

        input_hash: str | None = None
        output_hash: str | None = None
        if cfg.receipt_visibility == "proof_only":
            input_hash = hashlib.sha256(
                canonical_json(
                    {"_": inputs},
                    float_handling=cfg.float_handling,
                    float_precision=cfg.float_precision,
                )
            ).hexdigest()
            output_hash = hashlib.sha256(
                canonical_json(
                    {"_": output},
                    float_handling=cfg.float_handling,
                    float_precision=cfg.float_precision,
                )
            ).hexdigest()
            inputs = None
            output = None

        with self._lock:
            self._seq += 1
            seq = self._seq

            if self._prev_hash is None:
                prev_hash = compute_chain_anchor(cfg.key_secret, self._session_id)
            else:
                prev_hash = self._prev_hash

            duration_ms = int((ended_at - started_at).total_seconds() * 1000)
            reference_id = str(uuid.uuid4())

            receipt: dict[str, Any] = {
                "reference_id": reference_id,
                "session_id": self._session_id,
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
                "invocation_id": invocation_id,
                "sdk_version": __version__,
                "framework": framework,
                "framework_version": framework_version,
                "receipt_visibility": cfg.receipt_visibility,
            }
            if cfg.receipt_visibility == "proof_only":
                receipt["input_hash"] = input_hash
                receipt["output_hash"] = output_hash

            receipt_bytes = canonical_json(
                receipt,
                float_handling=cfg.float_handling,
                float_precision=cfg.float_precision,
            )
            if cfg.auth_version == 2 and self._ecdsa_private_key is not None:
                receipt["payload_hmac"] = ecdsa_sign_payload_v2(
                    self._ecdsa_private_key, receipt_bytes,
                )
            else:
                assert self._payload_key is not None
                receipt["payload_hmac"] = compute_hmac(self._payload_key, receipt_bytes)

            # Update chain: hash the complete receipt (with payload_hmac) for next prev_hash
            full_bytes = canonical_json(
                receipt,
                float_handling=cfg.float_handling,
                float_precision=cfg.float_precision,
            )
            self._prev_hash = compute_receipt_hash(full_bytes)

            # ``receipt`` is built as a ``dict[str, Any]`` so that the
            # conditional ``proof_only`` keys and the post-hoc ``payload_hmac``
            # can be added incrementally; ``cast`` asserts the finished builder
            # output conforms to the ``ReceiptPayload`` schema (a typed boundary,
            # not a blanket error-suppression).
            return cast(ReceiptPayload, receipt)

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def session_id(self) -> str:
        return self._session_id
