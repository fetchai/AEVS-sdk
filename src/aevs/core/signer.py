from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from aevs.config import AEVSConfig
from aevs.crypto.hkdf import derive_key
from aevs.crypto.hmac_auth import compute_hmac


def sign_request(
    config: AEVSConfig,
    payload_bytes: bytes,
    *,
    timestamp: datetime | None = None,
    signing_key: bytes | None = None,
) -> dict[str, str]:
    """Compute AEVS request authentication headers.

    Returns a dict with X-AEVS-Key-Id, X-AEVS-Timestamp, X-AEVS-Signature.
    The signature covers the timestamp and SHA-256 of the payload, preventing
    replay and tampering.

    Pass a pre-derived ``signing_key`` to avoid HKDF on every call.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    ts_str = timestamp.isoformat()
    if signing_key is None:
        signing_key = derive_key(config.key_secret, salt="aevs-request-v1")
    payload_hash = hashlib.sha256(payload_bytes).hexdigest()
    string_to_sign = f"{ts_str}\n{payload_hash}"
    signature = compute_hmac(signing_key, string_to_sign.encode("utf-8"))

    return {
        "X-AEVS-Key-Id": config.key_id,
        "X-AEVS-Timestamp": ts_str,
        "X-AEVS-Signature": signature,
    }
