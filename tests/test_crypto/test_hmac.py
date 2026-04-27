from aevs.crypto.hmac_auth import compute_hmac, verify_hmac


class TestHmac:
    def test_deterministic(self):
        sig1 = compute_hmac(b"key", b"message")
        sig2 = compute_hmac(b"key", b"message")
        assert sig1 == sig2

    def test_hex_string(self):
        sig = compute_hmac(b"key", b"message")
        assert isinstance(sig, str)
        assert len(sig) == 64  # SHA-256 hex = 64 chars
        int(sig, 16)  # must be valid hex

    def test_different_keys(self):
        s1 = compute_hmac(b"key-a", b"message")
        s2 = compute_hmac(b"key-b", b"message")
        assert s1 != s2

    def test_different_messages(self):
        s1 = compute_hmac(b"key", b"msg-a")
        s2 = compute_hmac(b"key", b"msg-b")
        assert s1 != s2


class TestVerifyHmac:
    def test_valid(self):
        sig = compute_hmac(b"key", b"message")
        assert verify_hmac(b"key", b"message", sig) is True

    def test_wrong_signature(self):
        assert verify_hmac(b"key", b"message", "0" * 64) is False

    def test_wrong_key(self):
        sig = compute_hmac(b"key-a", b"message")
        assert verify_hmac(b"key-b", b"message", sig) is False

    def test_wrong_message(self):
        sig = compute_hmac(b"key", b"msg-a")
        assert verify_hmac(b"key", b"msg-b", sig) is False
