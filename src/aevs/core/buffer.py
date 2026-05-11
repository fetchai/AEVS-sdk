from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from aevs.core.types import ReceiptPayload
from aevs.crypto.chain import compute_key_fingerprint, compute_receipt_hash
from aevs.crypto.hkdf import derive_key

logger = logging.getLogger("aevs")

_NONCE_SIZE = 12  # 96-bit nonce for AES-GCM


class LocalBuffer:
    """SQLite-backed local buffer for offline receipt storage.

    Receipts are AES-256-GCM encrypted at rest. A hash chain across records
    provides tamper evidence on flush. Thread-safe.
    """

    def __init__(
        self,
        db_path: Path,
        key_secret: bytes,
        *,
        max_records: int = 10_000,
    ) -> None:
        self._db_path = db_path.expanduser().resolve()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._enc_key = derive_key(key_secret, salt="aevs-encrypt-v1")
        self._aesgcm = AESGCM(self._enc_key)
        # Cache the deterministic key fingerprint so we can stamp it on
        # every chain_state write and reject mismatches on resume after a
        # key rotation (see chain_state() / store()).  The on-disk column
        # is still called ``key_anchor`` for back-compat with rows written
        # by older SDK versions; the value matches because both use the
        # same HKDF salt.
        self._key_fingerprint = compute_key_fingerprint(key_secret)
        self._max_records = max_records
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS receipts (
                seq         INTEGER PRIMARY KEY,
                receipt_enc BLOB    NOT NULL,
                prev_hash   TEXT    NOT NULL,
                status      TEXT    NOT NULL DEFAULT 'pending',
                created_at  TEXT    NOT NULL
            )
        """)
        # Single-row chain fingerprint that survives prune_flushed().  Lets
        # enable() resume the tamper-evident hash chain even after a clean
        # drain emptied the receipts table (production audit issue #18).
        # ``key_anchor`` lets us detect and reject a stale row left behind
        # by a previous key on the same buffer file.  ``session_id`` stamps
        # which UUIDv4 session the persisted state belongs to so a
        # mid-session crash recovery (pending_count > 0 on next enable())
        # can resume the same session rather than minting a new one.
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS chain_state (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                key_anchor TEXT    NOT NULL,
                last_seq   INTEGER NOT NULL,
                last_hash  TEXT    NOT NULL,
                session_id TEXT,
                updated_at TEXT    NOT NULL
            )
        """)
        # Idempotent migration for buffer files written by an earlier SDK
        # version that predates the session_id column.  ``ALTER TABLE``
        # raises ``OperationalError("duplicate column name")`` if the
        # column already exists; we catch and ignore.  Any other failure
        # (e.g. read-only filesystem) also degrades gracefully — the
        # buffer continues to work; ``chain_state()`` simply returns
        # ``session_id=None`` for legacy rows so callers fall back to
        # their normal "no session info available" path.
        try:
            self._conn.execute("ALTER TABLE chain_state ADD COLUMN session_id TEXT")
            logger.debug("AEVS: migrated chain_state table — added session_id column")
        except sqlite3.OperationalError:
            logger.debug("AEVS: chain_state.session_id column already exists, migration skipped")
        self._conn.commit()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def store(
        self,
        seq: int,
        payload_bytes: bytes,
        prev_hash: str,
        *,
        session_id: str | None = None,
    ) -> None:
        """Encrypt and store pre-serialized receipt bytes, evicting oldest if at capacity.

        ``payload_bytes`` must already be canonical-JSON-serialized with the
        correct config options.  The buffer stores these bytes verbatim so that
        the drainer can POST them without re-serialization.

        ``session_id`` (when provided) is persisted on the ``chain_state`` row
        so a future ``enable()`` with pending receipts can recover the same
        session and keep one linear chain across a crash.

        If eviction occurs, a ``status='gap'`` sentinel row is written for the
        evicted seq so that chain breaks are recorded locally rather than
        appearing as silent tampering.
        """
        encrypted = self._encrypt(payload_bytes)

        with self._lock:
            count = self._conn.execute(
                "SELECT COUNT(*) FROM receipts WHERE status = 'pending'"
            ).fetchone()[0]
            if count >= self._max_records:
                evicted_seq = self._conn.execute(
                    "SELECT MIN(seq) FROM receipts WHERE status = 'pending'"
                ).fetchone()[0]
                self._conn.execute(
                    "DELETE FROM receipts WHERE seq = ?", (evicted_seq,)
                )
                # Record a local gap sentinel so the chain break is auditable.
                # status='gap' rows are skipped by get_pending() and pruned with flushed rows.
                self._conn.execute(
                    "INSERT INTO receipts (seq, receipt_enc, prev_hash, status, created_at) "
                    "VALUES (?, ?, ?, 'gap', ?)",
                    (
                        evicted_seq,
                        b"",
                        f"EVICTED:{evicted_seq}",
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                logger.warning(
                    "AEVS: buffer at capacity (%d), evicted seq=%d — "
                    "hash chain integrity broken from this point; "
                    "increase max_buffer_records or reduce drain_interval_ms",
                    self._max_records,
                    evicted_seq,
                )

            now_iso = datetime.now(timezone.utc).isoformat()
            self._conn.execute(
                "INSERT INTO receipts (seq, receipt_enc, prev_hash, status, created_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (seq, encrypted, prev_hash, now_iso),
            )
            # Persist the post-receipt chain fingerprint so a future
            # enable() can resume the chain even if every receipt has
            # been flushed and pruned.  ``last_hash`` matches what the
            # next receipt's prev_hash should be (sha256 of canonical
            # JSON, exactly as ReceiptBuilder advances its own state).
            # The WHERE clause guards against any pathological out-of-
            # order call advancing state backwards.
            last_hash = compute_receipt_hash(payload_bytes)
            self._conn.execute(
                """
                INSERT INTO chain_state
                    (id, key_anchor, last_seq, last_hash, session_id, updated_at)
                VALUES (1, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    key_anchor = excluded.key_anchor,
                    last_seq   = excluded.last_seq,
                    last_hash  = excluded.last_hash,
                    session_id = excluded.session_id,
                    updated_at = excluded.updated_at
                WHERE excluded.last_seq > chain_state.last_seq
                """,
                (self._key_fingerprint, seq, last_hash, session_id, now_iso),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_pending(self) -> list[tuple[int, bytes]]:
        """Decrypt and return all pending receipts as ``(seq, payload_bytes)`` in order.

        The returned bytes are the original canonical-JSON payload passed to
        :meth:`store` — ready to POST to the backend without re-serialization.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, receipt_enc FROM receipts WHERE status = 'pending' ORDER BY seq"
            ).fetchall()

        results: list[tuple[int, bytes]] = []
        for seq, encrypted in rows:
            plaintext = self._decrypt(encrypted)
            results.append((seq, plaintext))
        return results

    def pending_count(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM receipts WHERE status = 'pending'"
            ).fetchone()
            return row[0] if row else 0

    def max_seq(self) -> int:
        """Return the highest seq number across all rows, or 0 if empty."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(seq) FROM receipts"
            ).fetchone()
            return row[0] if row[0] is not None else 0

    def last_receipt_bytes(self) -> bytes | None:
        """Decrypt and return the raw payload bytes of the highest-seq non-gap receipt."""
        with self._lock:
            row = self._conn.execute(
                "SELECT receipt_enc FROM receipts WHERE status != 'gap' ORDER BY seq DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return self._decrypt(row[0])

    def last_receipt(self) -> ReceiptPayload | None:
        """Decrypt, deserialize, and return the highest-seq non-gap receipt as a dict."""
        raw = self.last_receipt_bytes()
        if raw is None:
            return None
        result: ReceiptPayload = json.loads(raw)
        return result

    def reset_chain_state(self) -> None:
        """Delete the persisted chain fingerprint row.

        Called by ``enable()`` on a clean drain before minting a fresh
        ``session_id``.  Without this the next ``store()`` cannot
        overwrite the prior row — the UPSERT guard requires a strictly
        greater ``last_seq`` — so a crash before the new session
        surpasses the prior ``last_seq`` would mis-route recovery
        against the wrong session.

        Uses ``DELETE`` (not ``UPDATE ... SET session_id = NULL``) so
        :meth:`chain_state` returns ``None`` afterwards; a NULL
        ``session_id`` is reserved for legacy rows written before the
        column existed.
        """
        with self._lock:
            self._conn.execute("DELETE FROM chain_state WHERE id = 1")
            self._conn.commit()

    def chain_state(self) -> tuple[int, str, str | None] | None:
        """Return ``(last_seq, last_hash, session_id)`` of the most recent stored receipt.

        Survives :meth:`prune_flushed` so that ``enable()`` can resume the
        tamper-evident hash chain after a clean drain that emptied the
        ``receipts`` table.  Returns ``None`` when:

        * no receipt has ever been stored in this buffer, or
        * the persisted ``key_anchor`` does not match the current key
          (i.e. the buffer file was created by a different API key),
          which signals the caller to start a fresh chain rather than
          bridging two unrelated chains.

        ``session_id`` is ``None`` for rows written by an SDK version
        that predates the column (legacy buffer file).  Callers should
        treat that as "no session info available" and mint a fresh one.
        """
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT key_anchor, last_seq, last_hash, session_id "
                    "FROM chain_state WHERE id = 1"
                ).fetchone()
            except sqlite3.OperationalError:
                # ``session_id`` column unexpectedly absent (idempotent
                # ALTER in ``_init_schema`` failed silently, e.g.
                # read-only FS) — fall back to the legacy projection so
                # the buffer keeps working without surfacing into the
                # host agent.
                row = self._conn.execute(
                    "SELECT key_anchor, last_seq, last_hash FROM chain_state WHERE id = 1"
                ).fetchone()
                if row is None:
                    return None
                stored_anchor, last_seq, last_hash = row
                if stored_anchor != self._key_fingerprint:
                    return None
                return int(last_seq), str(last_hash), None
        if row is None:
            return None
        stored_anchor, last_seq, last_hash, session_id = row
        if stored_anchor != self._key_fingerprint:
            return None
        return int(last_seq), str(last_hash), session_id

    # ------------------------------------------------------------------
    # Flush lifecycle
    # ------------------------------------------------------------------

    def mark_flushed(self, seq_numbers: list[int]) -> None:
        if not seq_numbers:
            return
        placeholders = ",".join("?" * len(seq_numbers))
        with self._lock:
            self._conn.execute(
                f"UPDATE receipts SET status = 'flushed' WHERE seq IN ({placeholders})",
                seq_numbers,
            )
            self._conn.commit()

    def prune_flushed(self) -> int:
        """Delete flushed and gap records. Returns number deleted."""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM receipts WHERE status IN ('flushed', 'gap')"
            )
            self._conn.commit()
            return cursor.rowcount

    # ------------------------------------------------------------------
    # Encryption
    # ------------------------------------------------------------------

    def _encrypt(self, plaintext: bytes) -> bytes:
        nonce = os.urandom(_NONCE_SIZE)
        ciphertext = self._aesgcm.encrypt(nonce, plaintext, None)
        return nonce + ciphertext

    def _decrypt(self, data: bytes) -> bytes:
        nonce = data[:_NONCE_SIZE]
        ciphertext = data[_NONCE_SIZE:]
        return self._aesgcm.decrypt(nonce, ciphertext, None)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._conn.close()
