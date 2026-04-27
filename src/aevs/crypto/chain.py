import hashlib

from aevs.crypto.hkdf import derive_key


def compute_chain_anchor(key_secret: bytes) -> str:
    """Compute the initial prev_hash for seq=1.

    Derived deterministically from the API key secret so both SDK and
    backend can independently compute the same anchor.
    """
    seed = derive_key(key_secret, salt="aevs-chain-v1")
    return hashlib.sha256(seed).hexdigest()


def compute_receipt_hash(receipt_bytes: bytes) -> str:
    """SHA-256 hash of a receipt's canonical JSON.

    Used as prev_hash for the next receipt in the chain.
    """
    return hashlib.sha256(receipt_bytes).hexdigest()
