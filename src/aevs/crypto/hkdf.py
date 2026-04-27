from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


def derive_key(secret: bytes, *, salt: str, length: int = 32) -> bytes:
    """Derive a purpose-specific key using HKDF-SHA256.

    Each salt produces an independent derived key from the same master secret.
    Purpose separation is via the salt (see design doc §3.4 for the salt table).
    """
    return HKDF(
        algorithm=SHA256(),
        length=length,
        salt=salt.encode("utf-8"),
        info=b"",
    ).derive(secret)
