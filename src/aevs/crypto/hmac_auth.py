import hashlib
import hmac


def compute_hmac(key: bytes, message: bytes) -> str:
    """Compute HMAC-SHA256, return hex digest."""
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def verify_hmac(key: bytes, message: bytes, expected: str) -> bool:
    """Verify HMAC-SHA256 with constant-time comparison."""
    return hmac.compare_digest(compute_hmac(key, message), expected)
