from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from aevs.config import AEVSConfig
from aevs.crypto.ecdsa import _private_key_from_hex, ecdsa_sign_request_v2
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

    v1 (``aevs_sk_``): HMAC-SHA256 over ``"<ts>\\n<sha256_hex(body)>"``.
    v2 (``aevs_sk2_``): ECDSA P-256 ``r||s`` hex over the same message.
    """
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)

    ts_str = timestamp.isoformat()

    if config.auth_version == 2:
        private_key = _private_key_from_hex(config.key_secret.hex())
        signature = ecdsa_sign_request_v2(private_key, ts_str, payload_bytes)
    else:
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
