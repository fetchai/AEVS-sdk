from aevs.crypto.chain import compute_chain_anchor, compute_receipt_hash


class TestChainAnchor:
    def test_deterministic(self):
        a1 = compute_chain_anchor(b"secret")
        a2 = compute_chain_anchor(b"secret")
        assert a1 == a2

    def test_hex_string(self):
        anchor = compute_chain_anchor(b"secret")
        assert isinstance(anchor, str)
        assert len(anchor) == 64
        int(anchor, 16)

    def test_different_secrets(self):
        a1 = compute_chain_anchor(b"secret-a")
        a2 = compute_chain_anchor(b"secret-b")
        assert a1 != a2


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
