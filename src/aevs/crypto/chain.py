import hashlib

from aevs.crypto.hkdf import derive_key


def compute_key_fingerprint(key_secret: bytes) -> str:
    """Stable, key-derived identifier used by ``LocalBuffer`` to detect a
    buffer file being re-opened under a different API key.

    Intentionally uses the same HKDF salt the legacy ``compute_chain_anchor``
    used (``aevs-chain-v1``) so chain_state rows written by older SDK
    versions remain readable post-upgrade.  Never used as a receipt anchor.
    """
    seed = derive_key(key_secret, salt="aevs-chain-v1")
    return hashlib.sha256(seed).hexdigest()


def compute_chain_anchor(key_secret: bytes, session_id: str) -> str:
    """Compute the initial ``prev_hash`` for ``seq=1`` within an
    ``enable()`` session.

    Session-scoped: the same key plus a different ``session_id`` yields a
    different anchor.  Two SDK processes that share a key cannot collide
    on session_id (UUIDv4) and therefore cannot accidentally produce
    overlapping chains.  The function is deterministic so a backend
    verifier given ``(key_secret, session_id)`` can recompute the same
    anchor.
    """
    salt = f"aevs-chain-v1|{session_id}"
    seed = derive_key(key_secret, salt=salt)
    return hashlib.sha256(seed).hexdigest()


def compute_receipt_hash(receipt_bytes: bytes) -> str:
    """SHA-256 hash of a receipt's canonical JSON.

    Used as ``prev_hash`` for the next receipt in the chain.
    """
    return hashlib.sha256(receipt_bytes).hexdigest()
