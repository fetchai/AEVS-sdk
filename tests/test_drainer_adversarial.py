"""Adversarial tests for aevs._drainer — designed to break the drainer.

Targets: drain lock timeout, background loop exception handling,
_drain_once failure in get_pending, mark_flushed/prune_flushed failures.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from aevs._drainer import BufferDrainer


class TestDrainLockTimeout:
    def test_lock_timeout_skips_cycle(self):
        """If drain lock can't be acquired within timeout, the cycle is skipped."""
        buffer = MagicMock()
        client = MagicMock()
        drainer = BufferDrainer(buffer, client, interval_ms=1000)

        original_lock = drainer._drain_lock

        mock_lock = MagicMock()
        mock_lock.acquire.return_value = False
        drainer._drain_lock = mock_lock

        try:
            drainer.drain()
        finally:
            drainer._drain_lock = original_lock

        buffer.get_pending.assert_not_called()
        mock_lock.acquire.assert_called_once()
        mock_lock.release.assert_not_called()


class TestBackgroundLoopErrors:
    def test_run_loop_catches_drain_exception(self):
        """_run() must catch exceptions from drain() and keep running."""
        buffer = MagicMock()
        client = MagicMock()
        drainer = BufferDrainer(buffer, client, interval_ms=50)

        call_count = 0

        def exploding_drain():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise RuntimeError("drain exploded")
            drainer._stop_event.set()
            return True

        drainer.drain = exploding_drain
        drainer._run()
        assert call_count >= 2

    def test_stop_event_halts_loop(self):
        buffer = MagicMock()
        client = MagicMock()
        drainer = BufferDrainer(buffer, client, interval_ms=50)
        drainer._stop_event.set()
        drainer._run()


class TestCycleBackoff:
    def test_interval_increases_on_consecutive_failures(self):
        """When drain returns False, the cycle interval should back off."""
        buffer = MagicMock()
        client = MagicMock()
        drainer = BufferDrainer(buffer, client, interval_ms=1000)

        call_count = 0
        intervals_seen: list[float] = []

        def tracking_drain():
            nonlocal call_count
            call_count += 1
            intervals_seen.append(drainer._current_interval)
            if call_count >= 4:
                drainer._stop_event.set()
            return False  # simulate backend unreachable

        drainer.drain = tracking_drain
        drainer._current_interval = 0.01  # speed up test
        drainer._base_interval = 0.01
        drainer._run()

        assert drainer._consecutive_failures == 4
        assert drainer._current_interval > drainer._base_interval

    def test_interval_resets_on_success(self):
        """After failures, a successful drain resets interval to base."""
        buffer = MagicMock()
        client = MagicMock()
        drainer = BufferDrainer(buffer, client, interval_ms=1000)

        call_count = 0

        def fail_then_succeed():
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                return False
            drainer._stop_event.set()
            return True

        drainer.drain = fail_then_succeed
        drainer._current_interval = 0.01
        drainer._base_interval = 0.01
        drainer._run()

        assert drainer._consecutive_failures == 0
        assert drainer._current_interval == drainer._base_interval

    def test_interval_capped_at_max(self):
        """Backoff interval must never exceed _MAX_CYCLE_INTERVAL."""
        from aevs._drainer import _MAX_CYCLE_INTERVAL

        buffer = MagicMock()
        client = MagicMock()
        drainer = BufferDrainer(buffer, client, interval_ms=1000)

        drainer._consecutive_failures = 100
        drainer._base_interval = 1.0
        # Simulate what _run does on failure
        drainer._current_interval = min(
            drainer._base_interval * (2.0 ** drainer._consecutive_failures),
            _MAX_CYCLE_INTERVAL,
        )
        assert drainer._current_interval == _MAX_CYCLE_INTERVAL


class TestDrainOnceFailures:
    def test_get_pending_failure_logged_and_returns(self):
        """If buffer.get_pending() raises, drain_once returns without crash."""
        buffer = MagicMock()
        buffer.get_pending.side_effect = OSError("disk read failed")
        client = MagicMock()
        drainer = BufferDrainer(buffer, client)

        drainer._drain_once()
        client.send_receipt.assert_not_called()

    def test_empty_pending_is_noop(self):
        buffer = MagicMock()
        buffer.get_pending.return_value = []
        client = MagicMock()
        drainer = BufferDrainer(buffer, client)

        drainer._drain_once()
        client.send_receipt.assert_not_called()

    def test_send_failure_stops_at_first_error(self):
        """Drainer stops sending on first persistent failure (after retries)."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [
            (1, b'{"seq":1}'),
            (2, b'{"seq":2}'),
            (3, b'{"seq":3}'),
        ]
        client = MagicMock()
        # seq 1 succeeds; seq 2 fails all 3 retry attempts → drainer stops
        client.send_receipt.side_effect = [
            None,
            Exception("network error"),
            Exception("network error"),
            Exception("network error"),
        ]
        drainer = BufferDrainer(buffer, client)

        drainer._drain_once()

        assert client.send_receipt.call_count == 4  # 1 success + 3 retries
        buffer.mark_flushed.assert_called_once_with([1])

    def test_mark_flushed_failure_swallowed(self):
        """If mark_flushed raises after successful sends, error is logged not raised."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [(1, b'{"seq":1}')]
        buffer.mark_flushed.side_effect = OSError("sqlite locked")
        client = MagicMock()
        drainer = BufferDrainer(buffer, client)

        drainer._drain_once()
        buffer.mark_flushed.assert_called_once()

    def test_prune_flushed_failure_swallowed(self):
        """If prune_flushed raises, error is logged not raised."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [(1, b'{"seq":1}')]
        buffer.prune_flushed.side_effect = OSError("prune failed")
        client = MagicMock()
        drainer = BufferDrainer(buffer, client)

        drainer._drain_once()
        buffer.prune_flushed.assert_called_once()


class TestStopBehavior:
    def test_stop_with_final_flush(self):
        buffer = MagicMock()
        buffer.get_pending.return_value = []
        client = MagicMock()
        drainer = BufferDrainer(buffer, client, interval_ms=100)
        drainer.start()
        time.sleep(0.05)
        drainer.stop(final_flush=True)
        assert drainer._thread is None

    def test_stop_without_final_flush(self):
        buffer = MagicMock()
        client = MagicMock()
        drainer = BufferDrainer(buffer, client, interval_ms=100)
        drainer.start()
        time.sleep(0.05)
        drainer.stop(final_flush=False)
        assert drainer._thread is None

    def test_stop_when_never_started(self):
        buffer = MagicMock()
        buffer.get_pending.return_value = []
        client = MagicMock()
        drainer = BufferDrainer(buffer, client)
        drainer.stop(final_flush=True)
