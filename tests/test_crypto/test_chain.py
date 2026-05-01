from aevs.crypto.chain import (
    compute_chain_anchor,
    compute_key_fingerprint,
    compute_receipt_hash,
)

_SESSION_A = "11111111-1111-4111-8111-111111111111"
_SESSION_B = "22222222-2222-4222-8222-222222222222"


class TestChainAnchor:
    def test_deterministic(self):
        a1 = compute_chain_anchor(b"secret", _SESSION_A)
        a2 = compute_chain_anchor(b"secret", _SESSION_A)
        assert a1 == a2

    def test_hex_string(self):
        anchor = compute_chain_anchor(b"secret", _SESSION_A)
        assert isinstance(anchor, str)
        assert len(anchor) == 64
        int(anchor, 16)

    def test_different_secrets(self):
        a1 = compute_chain_anchor(b"secret-a", _SESSION_A)
        a2 = compute_chain_anchor(b"secret-b", _SESSION_A)
        assert a1 != a2

    def test_different_session_ids(self):
        """Same key + different session_id must yield distinct anchors —
        this is the cryptographic isolation that prevents two SDK
        processes sharing a key from forking the chain."""
        a1 = compute_chain_anchor(b"secret", _SESSION_A)
        a2 = compute_chain_anchor(b"secret", _SESSION_B)
        assert a1 != a2

    def test_anchor_distinct_from_key_fingerprint(self):
        """Belt-and-braces: chain anchors must never collide with the
        buffer's key fingerprint, even though both derive from the same
        key, because they serve different verification purposes."""
        anchor = compute_chain_anchor(b"secret", _SESSION_A)
        fingerprint = compute_key_fingerprint(b"secret")
        assert anchor != fingerprint


class TestKeyFingerprint:
    def test_deterministic(self):
        f1 = compute_key_fingerprint(b"secret")
        f2 = compute_key_fingerprint(b"secret")
        assert f1 == f2

    def test_session_independent(self):
        """The fingerprint exists to detect key rotation on a buffer
        file; it intentionally has no session_id parameter."""
        from inspect import signature
        assert "session_id" not in signature(compute_key_fingerprint).parameters

    def test_different_secrets(self):
        assert compute_key_fingerprint(b"a") != compute_key_fingerprint(b"b")


class TestReceiptHash:
    def test_deterministic(self):
        h1 = compute_receipt_hash(b'{"seq":1}')
        h2 = compute_receipt_hash(b'{"seq":1}')
        assert h1 == h2

    def test_hex_string(self):
        h = compute_receipt_hash(b'{"seq":1}')
        assert len(h) == 64
        int(h, 16)

    def test_different_data(self):
        h1 = compute_receipt_hash(b'{"seq":1}')
        h2 = compute_receipt_hash(b'{"seq":2}')
        assert h1 != h2
