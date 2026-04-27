import json
from pathlib import Path

import pytest

from aevs.core.buffer import LocalBuffer
from aevs.core.serializer import canonical_json
from tests.conftest import TEST_KEY_SECRET


@pytest.fixture
def buffer(tmp_path: Path) -> LocalBuffer:
    buf = LocalBuffer(tmp_path / "test_buffer.db", TEST_KEY_SECRET)
    yield buf
    buf.close()


def _make_receipt(seq: int, tool: str = "search") -> dict:
    return {
        "seq": seq,
        "tool_name": tool,
        "inputs": {"q": "test"},
        "output": {"r": "result"},
        "status": "success",
        "payload_hmac": "abc123",
        "prev_hash": "000" * 21 + "00",
    }


def _to_bytes(receipt: dict) -> bytes:
    """Serialize a receipt dict to canonical JSON bytes (matching SDK handler behavior)."""
    return canonical_json(receipt)


class TestLocalBuffer:
    def test_store_and_retrieve(self, buffer: LocalBuffer):
        receipt = _make_receipt(1)
        payload = _to_bytes(receipt)
        buffer.store(1, payload, prev_hash="anchor")
        pending = buffer.get_pending()
        assert len(pending) == 1
        seq, data = pending[0]
        assert seq == 1
        assert data == payload

    def test_preserves_order(self, buffer: LocalBuffer):
        for i in range(1, 6):
            r = _make_receipt(i, tool=f"tool_{i}")
            buffer.store(i, _to_bytes(r), prev_hash="h")
        pending = buffer.get_pending()
        assert [seq for seq, _ in pending] == [1, 2, 3, 4, 5]

    def test_pending_count(self, buffer: LocalBuffer):
        assert buffer.pending_count() == 0
        buffer.store(1, _to_bytes(_make_receipt(1)), prev_hash="h")
        buffer.store(2, _to_bytes(_make_receipt(2)), prev_hash="h")
        assert buffer.pending_count() == 2

    def test_mark_flushed(self, buffer: LocalBuffer):
        for i in range(1, 4):
            buffer.store(i, _to_bytes(_make_receipt(i)), prev_hash="h")

        buffer.mark_flushed([1, 2])
        pending = buffer.get_pending()
        assert len(pending) == 1
        assert pending[0][0] == 3

    def test_prune_flushed(self, buffer: LocalBuffer):
        buffer.store(1, _to_bytes(_make_receipt(1)), prev_hash="h")
        buffer.store(2, _to_bytes(_make_receipt(2)), prev_hash="h")
        buffer.mark_flushed([1, 2])

        deleted = buffer.prune_flushed()
        assert deleted == 2
        assert buffer.pending_count() == 0

    def test_prune_empty(self, buffer: LocalBuffer):
        assert buffer.prune_flushed() == 0

    def test_mark_flushed_empty_list(self, buffer: LocalBuffer):
        buffer.mark_flushed([])  # should not raise


class TestBufferMaxRecords:
    def test_evicts_oldest_when_at_capacity(self, tmp_path):
        buf = LocalBuffer(tmp_path / "cap.db", TEST_KEY_SECRET, max_records=3)
        for i in range(1, 4):
            buf.store(i, _to_bytes(_make_receipt(i)), prev_hash="h")
        assert buf.pending_count() == 3

        buf.store(4, _to_bytes(_make_receipt(4)), prev_hash="h")
        assert buf.pending_count() == 3
        pending = buf.get_pending()
        seqs = [seq for seq, _ in pending]
        assert seqs == [2, 3, 4]
        buf.close()

    def test_evicts_multiple_times(self, tmp_path):
        buf = LocalBuffer(tmp_path / "cap2.db", TEST_KEY_SECRET, max_records=2)
        for i in range(1, 5):
            buf.store(i, _to_bytes(_make_receipt(i)), prev_hash="h")
        pending = buf.get_pending()
        assert [seq for seq, _ in pending] == [3, 4]
        buf.close()


class TestBufferEncryption:
    def test_data_encrypted_on_disk(self, buffer: LocalBuffer, tmp_path: Path):
        payload = _to_bytes(_make_receipt(1))
        buffer.store(1, payload, prev_hash="h")

        import sqlite3

        conn = sqlite3.connect(str(tmp_path / "test_buffer.db"))
        row = conn.execute("SELECT receipt_enc FROM receipts WHERE seq = 1").fetchone()
        conn.close()

        raw = row[0]
        with pytest.raises(Exception):
            json.loads(raw)

    def test_wrong_key_cannot_decrypt(self, tmp_path: Path):
        buf1 = LocalBuffer(tmp_path / "enc_test.db", b"secret_a" * 4)
        buf1.store(1, _to_bytes(_make_receipt(1)), prev_hash="h")
        buf1.close()

        buf2 = LocalBuffer(tmp_path / "enc_test.db", b"secret_b" * 4)
        with pytest.raises(Exception):
            buf2.get_pending()
        buf2.close()


class TestBufferPersistence:
    def test_survives_reopen(self, tmp_path: Path):
        db_path = tmp_path / "persist.db"

        buf = LocalBuffer(db_path, TEST_KEY_SECRET)
        for i in (1, 2):
            buf.store(i, _to_bytes(_make_receipt(i)), prev_hash="h")
        buf.close()

        buf2 = LocalBuffer(db_path, TEST_KEY_SECRET)
        pending = buf2.get_pending()
        assert len(pending) == 2
        assert pending[0][0] == 1
        buf2.close()

    def test_creates_parent_dirs(self, tmp_path: Path):
        deep_path = tmp_path / "a" / "b" / "c" / "buffer.db"
        buf = LocalBuffer(deep_path, TEST_KEY_SECRET)
        buf.store(1, _to_bytes(_make_receipt(1)), prev_hash="h")
        assert buf.pending_count() == 1
        buf.close()


class TestBufferMaxSeq:
    def test_max_seq_empty(self, buffer: LocalBuffer):
        assert buffer.max_seq() == 0

    def test_max_seq_with_records(self, buffer: LocalBuffer):
        for i in (1, 5, 3):
            buffer.store(i, _to_bytes(_make_receipt(i)), prev_hash="h")
        assert buffer.max_seq() == 5

    def test_max_seq_includes_flushed(self, buffer: LocalBuffer):
        buffer.store(1, _to_bytes(_make_receipt(1)), prev_hash="h")
        buffer.store(2, _to_bytes(_make_receipt(2)), prev_hash="h")
        buffer.mark_flushed([1])
        assert buffer.max_seq() == 2

    def test_max_seq_after_prune(self, buffer: LocalBuffer):
        buffer.store(1, _to_bytes(_make_receipt(1)), prev_hash="h")
        buffer.store(2, _to_bytes(_make_receipt(2)), prev_hash="h")
        buffer.mark_flushed([1])
        buffer.prune_flushed()
        assert buffer.max_seq() == 2


class TestBufferLastReceipt:
    def test_last_receipt_bytes_empty(self, buffer: LocalBuffer):
        assert buffer.last_receipt_bytes() is None

    def test_last_receipt_bytes_returns_highest_seq(self, buffer: LocalBuffer):
        for i, tool in [(1, "first"), (3, "third"), (2, "second")]:
            buffer.store(i, _to_bytes(_make_receipt(i, tool=tool)), prev_hash="h")
        raw = buffer.last_receipt_bytes()
        assert raw is not None
        parsed = json.loads(raw)
        assert parsed["seq"] == 3
        assert parsed["tool_name"] == "third"

    def test_last_receipt_dict(self, buffer: LocalBuffer):
        buffer.store(1, _to_bytes(_make_receipt(1, tool="only")), prev_hash="h")
        last = buffer.last_receipt()
        assert last is not None
        assert last["seq"] == 1
        assert last["tool_name"] == "only"

    def test_last_receipt_empty(self, buffer: LocalBuffer):
        assert buffer.last_receipt() is None

    def test_last_receipt_survives_reopen(self, tmp_path: Path):
        db_path = tmp_path / "lr.db"
        buf = LocalBuffer(db_path, TEST_KEY_SECRET)
        buf.store(1, _to_bytes(_make_receipt(1)), prev_hash="h")
        buf.store(2, _to_bytes(_make_receipt(2, tool="last_tool")), prev_hash="h")
        buf.close()

        buf2 = LocalBuffer(db_path, TEST_KEY_SECRET)
        last = buf2.last_receipt()
        assert last is not None
        assert last["seq"] == 2
        assert last["tool_name"] == "last_tool"
        buf2.close()
