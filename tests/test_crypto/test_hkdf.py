from aevs.crypto.hkdf import derive_key


class TestDeriveKey:
    def test_returns_correct_length(self):
        key = derive_key(b"secret", salt="test-salt")
        assert len(key) == 32

    def test_custom_length(self):
        key = derive_key(b"secret", salt="test-salt", length=16)
        assert len(key) == 16

    def test_deterministic(self):
        k1 = derive_key(b"secret", salt="test-salt")
        k2 = derive_key(b"secret", salt="test-salt")
        assert k1 == k2

    def test_different_salts_different_keys(self):
        k1 = derive_key(b"secret", salt="aevs-payload-v1")
        k2 = derive_key(b"secret", salt="aevs-request-v1")
        assert k1 != k2

    def test_different_secrets_different_keys(self):
        k1 = derive_key(b"secret-a", salt="test-salt")
        k2 = derive_key(b"secret-b", salt="test-salt")
        assert k1 != k2
