"""ECDSA P-256 / SHA-256 signing utilities for SDK auth v2.

All signatures are transmitted as raw ``r || s`` hex (128 hex chars = 64 bytes).
"""

from __future__ import annotations

import hashlib

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils as asym_utils


def _der_to_rs_hex(der_sig: bytes) -> str:
    r, s = asym_utils.decode_dss_signature(der_sig)
    return r.to_bytes(32, "big").hex() + s.to_bytes(32, "big").hex()


def _private_key_from_hex(hex_secret: str) -> ec.EllipticCurvePrivateKey:
    scalar = int.from_bytes(bytes.fromhex(hex_secret), "big")
    return ec.derive_private_key(scalar, ec.SECP256R1())


def ecdsa_sign(private_key: ec.EllipticCurvePrivateKey, message: bytes) -> str:
    """Sign *message* with ECDSA P-256 / SHA-256, return ``r||s`` hex."""
    digest = hashlib.sha256(message).digest()
    der_sig = private_key.sign(
        digest,
        ec.ECDSA(asym_utils.Prehashed(hashes.SHA256())),
    )
    return _der_to_rs_hex(der_sig)


def ecdsa_sign_request_v2(
    private_key: ec.EllipticCurvePrivateKey,
    timestamp_str: str,
    payload_bytes: bytes,
) -> str:
    """Sign ``"<ts>\\n<sha256_hex(body)>"`` with ECDSA P-256, return ``r||s`` hex."""
    payload_hash = hashlib.sha256(payload_bytes).hexdigest()
    string_to_sign = f"{timestamp_str}\n{payload_hash}".encode("utf-8")
    return ecdsa_sign(private_key, string_to_sign)


def ecdsa_sign_payload_v2(
    private_key: ec.EllipticCurvePrivateKey,
    canonical_payload_bytes: bytes,
) -> str:
    """Sign canonical payload bytes with ECDSA P-256, return ``r||s`` hex."""
    return ecdsa_sign(private_key, canonical_payload_bytes)
