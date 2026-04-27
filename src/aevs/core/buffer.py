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
        self._conn.commit()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def store(self, seq: int, payload_bytes: bytes, prev_hash: str) -> None:
        """Encrypt and store pre-serialized receipt bytes, evicting oldest if at capacity.

        ``payload_bytes`` must already be canonical-JSON-serialized with the
        correct config options.  The buffer stores these bytes verbatim so that
        the drainer can POST them without re-serialization.

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

            self._conn.execute(
                "INSERT INTO receipts (seq, receipt_enc, prev_hash, status, created_at) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (
                    seq,
                    encrypted,
                    prev_hash,
                    datetime.now(timezone.utc).isoformat(),
                ),
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
