"""Background buffer drainer — periodically sends buffered receipts to the backend.

The drainer runs as a daemon thread started by ``enable()`` and stopped by
``disable()``.  ``flush()`` triggers an immediate synchronous drain cycle.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aevs.core.buffer import LocalBuffer
    from aevs.core.client import AEVSClient

logger = logging.getLogger("aevs")

_MAX_RETRIES = 3
_RETRY_BASE_DELAY = 0.1  # seconds; doubles each attempt: 0.1s, 0.2s, 0.4s
_BACKOFF_MULTIPLIER = 2.0
_MAX_CYCLE_INTERVAL = 300.0  # 5 minute cap on backoff between drain cycles


class BufferDrainer:
    """Drains pending receipts from the local buffer to the AEVS backend.

    A daemon thread wakes every ``interval_ms`` milliseconds, reads pending
    receipts from the buffer, sends them in sequence order, and marks them
    as flushed.  On the first persistent failure (after retries) the cycle
    stops; the failed receipt is retried at the next interval.

    When the backend is unreachable, the cycle interval backs off
    exponentially (up to ``_MAX_CYCLE_INTERVAL``) to avoid wasting
    resources.  It resets to the base interval on the first successful send.

    ``drain()`` may also be called directly for a synchronous flush.
    A ``threading.Lock`` ensures only one drain cycle runs at a time.
    The lock timeout is set to 60 s to accommodate retry back-off plus
    the configured HTTP signing timeout (``_MAX_RETRIES × signing_timeout_ms``
    must stay below this ceiling).
    """

    _DRAIN_LOCK_TIMEOUT = 60.0  # seconds

    def __init__(
        self,
        buffer: LocalBuffer,
        client: AEVSClient,
        *,
        interval_ms: int = 5_000,
    ) -> None:
        self._buffer = buffer
        self._client = client
        self._base_interval = interval_ms / 1000.0
        self._current_interval = self._base_interval
        self._consecutive_failures = 0
        self._stop_event = threading.Event()
        self._drain_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def _interval(self) -> float:
        return self._current_interval

    def start(self) -> None:
        """Start the background drainer thread."""
        self._stop_event.clear()
        self._current_interval = self._base_interval
        self._consecutive_failures = 0
        self._thread = threading.Thread(target=self._run, daemon=True, name="aevs-drainer")
        self._thread.start()

    def stop(self, *, final_flush: bool = True) -> None:
        """Signal the background thread to stop and optionally do a last drain."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        if final_flush:
            self.drain()

    def drain(self) -> bool:
        """Run one drain cycle synchronously. Thread-safe.

        Returns True if all pending receipts were sent (or none were pending),
        False if any receipt failed to send.
        """
        if not self._drain_lock.acquire(timeout=self._DRAIN_LOCK_TIMEOUT):
            logger.warning("AEVS: drain lock timeout, skipping cycle")
            return False
        try:
            return self._drain_once()
        finally:
            self._drain_lock.release()

    def _run(self) -> None:
        """Background loop: sleep, drain, repeat until stopped.

        Backs off exponentially when the backend is unreachable and
        resets to the base interval on the first successful cycle.
        """
        while not self._stop_event.wait(timeout=self._current_interval):
            try:
                success = self.drain()
                if success:
                    if self._consecutive_failures > 0:
                        logger.info(
                            "AEVS: backend reachable again after %d failed cycles",
                            self._consecutive_failures,
                        )
                    self._consecutive_failures = 0
                    self._current_interval = self._base_interval
                else:
                    self._consecutive_failures += 1
                    self._current_interval = min(
                        self._base_interval * (_BACKOFF_MULTIPLIER ** self._consecutive_failures),
                        _MAX_CYCLE_INTERVAL,
                    )
                    logger.warning(
                        "AEVS: drain failed, next retry in %.0fs (%d consecutive failures)",
                        self._current_interval,
                        self._consecutive_failures,
                    )
            except Exception:
                logger.error("AEVS: drainer cycle failed", exc_info=True)

    def _drain_once(self) -> bool:
        """Send pending receipts to the backend, stop on first persistent failure.

        Returns True if all receipts sent (or none pending), False otherwise.
        """
        try:
            pending = self._buffer.get_pending()
        except Exception:
            logger.error("AEVS: drainer failed to read pending receipts", exc_info=True)
            return False

        if not pending:
            return True

        flushed_seqs: list[int] = []
        all_sent = True
        for seq, payload_bytes in pending:
            sent = False
            for attempt in range(_MAX_RETRIES):
                try:
                    self._client.send_receipt(payload_bytes)
                    flushed_seqs.append(seq)
                    sent = True
                    break
                except Exception:
                    if attempt < _MAX_RETRIES - 1:
                        time.sleep(_RETRY_BASE_DELAY * (2 ** attempt))
                    else:
                        logger.debug(
                            "AEVS: drainer send failed at seq=%d after %d attempts, "
                            "will retry next cycle",
                            seq,
                            _MAX_RETRIES,
                        )
            if not sent:
                all_sent = False
                break

        if flushed_seqs:
            try:
                self._buffer.mark_flushed(flushed_seqs)
                self._buffer.prune_flushed()
            except Exception:
                logger.error(
                    "AEVS: drainer failed to mark/prune flushed receipts",
                    exc_info=True,
                )

        return all_sent
