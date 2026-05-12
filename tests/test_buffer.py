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


class TestBufferChainState:
    """Persistent chain_state survives prune_flushed() so enable() can
    resume the tamper-evident chain after a clean drain (audit issue #18)."""

    def test_chain_state_empty_when_never_stored(self, buffer: LocalBuffer):
        assert buffer.chain_state() is None

    def test_chain_state_returns_seq_and_hash_after_store(
        self, buffer: LocalBuffer
    ):
        from aevs.crypto.chain import compute_receipt_hash

        payload = _to_bytes(_make_receipt(1))
        buffer.store(1, payload, prev_hash="h")

        state = buffer.chain_state()
        assert state is not None
        seq, last_hash, session_id = state
        assert seq == 1
        assert last_hash == compute_receipt_hash(payload)
        # No session_id was supplied to store(), so the chain_state row
        # carries None — callers treat that as "legacy / unknown session".
        assert session_id is None

    def test_chain_state_advances_with_each_store(self, buffer: LocalBuffer):
        from aevs.crypto.chain import compute_receipt_hash

        for i in (1, 2, 3):
            payload = _to_bytes(_make_receipt(i))
            buffer.store(i, payload, prev_hash="h")

        state = buffer.chain_state()
        assert state is not None
        last_seq, last_hash, _ = state
        assert last_seq == 3
        # Matches the hash of the most-recently-stored receipt's bytes.
        assert last_hash == compute_receipt_hash(_to_bytes(_make_receipt(3)))

    def test_chain_state_survives_prune_flushed(self, tmp_path: Path):
        """Core regression for issue #18: after a full drain the chain
        fingerprint must still be readable so the next session resumes
        rather than restarting at seq=1."""
        from aevs.crypto.chain import compute_receipt_hash

        db_path = tmp_path / "drain.db"
        buf = LocalBuffer(db_path, TEST_KEY_SECRET)
        for i in (1, 2):
            buf.store(i, _to_bytes(_make_receipt(i)), prev_hash="h")
        buf.mark_flushed([1, 2])
        deleted = buf.prune_flushed()
        assert deleted == 2
        assert buf.pending_count() == 0
        assert buf.max_seq() == 0  # receipts table really is empty

        state = buf.chain_state()
        assert state is not None
        last_seq, last_hash, _ = state
        assert last_seq == 2
        assert last_hash == compute_receipt_hash(_to_bytes(_make_receipt(2)))
        buf.close()

    def test_chain_state_survives_reopen_after_drain(self, tmp_path: Path):
        from aevs.crypto.chain import compute_receipt_hash

        db_path = tmp_path / "reopen.db"
        buf = LocalBuffer(db_path, TEST_KEY_SECRET)
        buf.store(1, _to_bytes(_make_receipt(1)), prev_hash="h")
        buf.store(2, _to_bytes(_make_receipt(2)), prev_hash="h")
        buf.mark_flushed([1, 2])
        buf.prune_flushed()
        buf.close()

        buf2 = LocalBuffer(db_path, TEST_KEY_SECRET)
        state = buf2.chain_state()
        assert state is not None
        last_seq, last_hash, _ = state
        assert last_seq == 2
        assert last_hash == compute_receipt_hash(_to_bytes(_make_receipt(2)))
        buf2.close()

    def test_chain_state_returns_none_on_key_mismatch(self, tmp_path: Path):
        """A buffer file that was written by a different key must surface
        as ``chain_state() is None`` so callers do not bridge two
        unrelated chains across a key rotation."""
        db_path = tmp_path / "rotated.db"
        buf_a = LocalBuffer(db_path, b"key_a___" * 4)
        buf_a.store(1, _to_bytes(_make_receipt(1)), prev_hash="h")
        buf_a.close()

        buf_b = LocalBuffer(db_path, b"key_b___" * 4)
        assert buf_b.chain_state() is None
        buf_b.close()

    def test_chain_state_does_not_advance_backwards(self, buffer: LocalBuffer):
        """Defensive: a pathological out-of-order store at a lower seq
        must not rewind the persisted chain fingerprint."""
        from aevs.crypto.chain import compute_receipt_hash

        buffer.store(5, _to_bytes(_make_receipt(5)), prev_hash="h")
        forward_state = buffer.chain_state()
        assert forward_state is not None
        assert forward_state[0] == 5

        # Replay with a smaller seq — chain_state must stay at 5.
        buffer.store(2, _to_bytes(_make_receipt(2)), prev_hash="h")
        state = buffer.chain_state()
        assert state is not None
        last_seq, last_hash, _ = state
        assert last_seq == 5
        assert last_hash == compute_receipt_hash(_to_bytes(_make_receipt(5)))


class TestBufferChainStateSessionId:
    """``chain_state`` round-trips ``session_id`` and gracefully migrates
    legacy buffer files that predate the column."""

    _SESSION_A = "11111111-1111-4111-8111-aaaaaaaaaaaa"
    _SESSION_B = "22222222-2222-4222-8222-bbbbbbbbbbbb"

    def test_store_persists_session_id(self, buffer: LocalBuffer):
        buffer.store(
            1, _to_bytes(_make_receipt(1)), prev_hash="h", session_id=self._SESSION_A
        )
        state = buffer.chain_state()
        assert state is not None
        last_seq, _last_hash, persisted_session = state
        assert last_seq == 1
        assert persisted_session == self._SESSION_A

    def test_reset_chain_state_deletes_row_so_chain_state_is_none(
        self, buffer: LocalBuffer
    ):
        """``reset_chain_state()`` must DELETE (not null) so the next
        ``chain_state()`` reports the row absent — a NULL ``session_id``
        is reserved for legacy rows."""
        buffer.store(
            1, _to_bytes(_make_receipt(1)), prev_hash="h", session_id=self._SESSION_A
        )
        assert buffer.chain_state() is not None

        buffer.reset_chain_state()

        assert buffer.chain_state() is None

    def test_reset_then_store_writes_fresh_session_row(
        self, buffer: LocalBuffer
    ):
        """After reset, a lower-seq write for a new session must land —
        the UPSERT guard's monotonic check would otherwise block it."""
        buffer.store(
            50,
            _to_bytes(_make_receipt(50)),
            prev_hash="h",
            session_id=self._SESSION_A,
        )

        buffer.reset_chain_state()

        buffer.store(
            1, _to_bytes(_make_receipt(1)), prev_hash="h", session_id=self._SESSION_B
        )
        state = buffer.chain_state()
        assert state is not None
        last_seq, _last_hash, persisted_session = state
        assert last_seq == 1
        assert persisted_session == self._SESSION_B

    def test_store_advances_session_id_with_seq(self, buffer: LocalBuffer):
        """Mid-session crash recovery's correctness depends on the most
        recent session_id (not the first one) being persisted."""
        buffer.store(
            1, _to_bytes(_make_receipt(1)), prev_hash="h", session_id=self._SESSION_A
        )
        buffer.store(
            2, _to_bytes(_make_receipt(2)), prev_hash="h", session_id=self._SESSION_B
        )
        state = buffer.chain_state()
        assert state is not None
        _last_seq, _last_hash, persisted_session = state
        assert persisted_session == self._SESSION_B

    def test_store_without_session_id_writes_null(self, buffer: LocalBuffer):
        """A legacy caller that omits session_id keeps round-tripping
        ``None`` so callers can detect the legacy state."""
        buffer.store(1, _to_bytes(_make_receipt(1)), prev_hash="h")
        state = buffer.chain_state()
        assert state is not None
        assert state[2] is None

    def test_session_id_survives_prune_and_reopen(self, tmp_path: Path):
        db_path = tmp_path / "session.db"
        buf = LocalBuffer(db_path, TEST_KEY_SECRET)
        buf.store(
            1, _to_bytes(_make_receipt(1)), prev_hash="h", session_id=self._SESSION_A
        )
        buf.mark_flushed([1])
        buf.prune_flushed()
        buf.close()

        buf2 = LocalBuffer(db_path, TEST_KEY_SECRET)
        try:
            state = buf2.chain_state()
            assert state is not None
            assert state[2] == self._SESSION_A
        finally:
            buf2.close()

    def test_legacy_db_without_session_id_column_auto_migrates(self, tmp_path: Path):
        """An ``ALTER TABLE`` runs on every open so a buffer file written
        by the issue-#18-only SDK (no session_id column) keeps working
        without crashing the host."""
        import sqlite3

        db_path = tmp_path / "legacy.db"

        # Hand-craft the legacy schema: chain_state without session_id.
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            """
            CREATE TABLE receipts (
                seq INTEGER PRIMARY KEY,
                receipt_enc BLOB NOT NULL,
                prev_hash TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE chain_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                key_anchor TEXT NOT NULL,
                last_seq INTEGER NOT NULL,
                last_hash TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        # Insert a legacy chain_state row using the same key fingerprint
        # the new SDK will compute, so the key-rotation guard accepts it.
        from aevs.crypto.chain import compute_key_fingerprint

        conn.execute(
            "INSERT INTO chain_state (id, key_anchor, last_seq, last_hash, updated_at) "
            "VALUES (1, ?, ?, ?, ?)",
            (compute_key_fingerprint(TEST_KEY_SECRET), 7, "h" * 64, "2026-01-01T00:00:00Z"),
        )
        conn.commit()
        conn.close()

        buf = LocalBuffer(db_path, TEST_KEY_SECRET)
        try:
            # Open succeeds; legacy chain_state row is readable and reports
            # session_id=None so callers fall back to the "no session info"
            # branch instead of bridging onto an unknown session.
            state = buf.chain_state()
            assert state is not None
            last_seq, last_hash, session_id = state
            assert last_seq == 7
            assert last_hash == "h" * 64
            assert session_id is None

            # Subsequent stores can attach a session_id and the post-migration
            # column accepts it.
            buf.store(
                8,
                _to_bytes(_make_receipt(8)),
                prev_hash="h",
                session_id=self._SESSION_A,
            )
            new_state = buf.chain_state()
            assert new_state is not None
            assert new_state[2] == self._SESSION_A
        finally:
            buf.close()
