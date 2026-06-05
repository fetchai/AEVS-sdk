"""Adversarial tests for aevs._drainer — designed to break the drainer.

Targets: drain lock timeout, background loop exception handling,
_drain_once failure in get_pending, mark_flushed/prune_flushed failures,
batch sending behavior.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, PropertyMock

import httpx

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
    """One-by-one path failures — batch explicitly disabled to test fallback."""

    def test_get_pending_failure_logged_and_returns(self):
        """If buffer.get_pending() raises, drain_once returns without crash."""
        buffer = MagicMock()
        buffer.get_pending.side_effect = OSError("disk read failed")
        client = MagicMock()
        drainer = BufferDrainer(buffer, client, max_batch_size=0)

        result = drainer._drain_once()

        assert result is False
        client.send_receipt.assert_not_called()

    def test_empty_pending_is_noop(self):
        buffer = MagicMock()
        buffer.get_pending.return_value = []
        client = MagicMock()
        drainer = BufferDrainer(buffer, client, max_batch_size=0)

        result = drainer._drain_once()

        assert result is True
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
        client.send_receipt.side_effect = [
            None,
            Exception("network error"),
            Exception("network error"),
            Exception("network error"),
        ]
        drainer = BufferDrainer(buffer, client, max_batch_size=0)

        result = drainer._drain_once()

        assert result is False
        assert client.send_receipt.call_count == 4  # 1 success + 3 retries
        buffer.mark_flushed.assert_called_once_with([1])

    def test_mark_flushed_failure_swallowed(self):
        """If mark_flushed raises after successful sends, error is logged not raised."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [(1, b'{"seq":1}')]
        buffer.mark_flushed.side_effect = OSError("sqlite locked")
        client = MagicMock()
        drainer = BufferDrainer(buffer, client, max_batch_size=0)

        drainer._drain_once()
        buffer.mark_flushed.assert_called_once()

    def test_prune_flushed_failure_swallowed(self):
        """If prune_flushed raises, error is logged not raised."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [(1, b'{"seq":1}')]
        buffer.prune_flushed.side_effect = OSError("prune failed")
        client = MagicMock()
        drainer = BufferDrainer(buffer, client, max_batch_size=0)

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


# ---------------------------------------------------------------------------
# Batch sending tests
# ---------------------------------------------------------------------------


def _mock_batch_response(results, failed_at_index=None, status_code=200):
    """Build a mock httpx.Response for a batch endpoint call."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = {
        "results": results,
        "failed_at_index": failed_at_index,
    }
    resp.is_error = status_code >= 400
    return resp


def _http_status_error(status_code: int, text: str = "") -> httpx.HTTPStatusError:
    """Build an httpx.HTTPStatusError with the given status code."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.is_error = True
    resp.text = text or str(status_code)
    resp.headers = {"content-type": "text/plain"}
    return httpx.HTTPStatusError(str(status_code), request=MagicMock(), response=resp)


class TestBatchDrainSuccess:
    def test_batch_sends_all_pending(self):
        """All pending receipts sent in one batch call and marked flushed."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [
            (1, b'{"seq":1}'),
            (2, b'{"seq":2}'),
            (3, b'{"seq":3}'),
        ]
        client = MagicMock()
        client.send_receipts_batch.return_value = _mock_batch_response([
            {"status": "created", "receipt_id": "aaa", "reference_id": None},
            {"status": "created", "receipt_id": "bbb", "reference_id": None},
            {"status": "created", "receipt_id": "ccc", "reference_id": None},
        ])
        drainer = BufferDrainer(buffer, client, max_batch_size=50)

        result = drainer._drain_once()

        assert result is True
        client.send_receipts_batch.assert_called_once()
        client.send_receipt.assert_not_called()
        buffer.mark_flushed.assert_called_once_with([1, 2, 3])

    def test_batch_chunks_large_pending(self):
        """Pending receipts are split into chunks of max_batch_size."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [
            (i, f'{{"seq":{i}}}'.encode()) for i in range(1, 6)
        ]
        client = MagicMock()
        responses = [
            _mock_batch_response([
                {"status": "created", "receipt_id": "a1"},
                {"status": "created", "receipt_id": "a2"},
            ]),
            _mock_batch_response([
                {"status": "created", "receipt_id": "a3"},
                {"status": "created", "receipt_id": "a4"},
            ]),
            _mock_batch_response([
                {"status": "created", "receipt_id": "a5"},
            ]),
        ]
        client.send_receipts_batch.side_effect = responses
        drainer = BufferDrainer(buffer, client, max_batch_size=2)

        result = drainer._drain_once()

        assert result is True
        assert client.send_receipts_batch.call_count == 3
        assert buffer.mark_flushed.call_count == 3
        buffer.mark_flushed.assert_any_call([1, 2])
        buffer.mark_flushed.assert_any_call([3, 4])
        buffer.mark_flushed.assert_any_call([5])

    def test_batch_with_duplicates_marks_flushed(self):
        """Duplicate receipts are still marked flushed."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [
            (1, b'{"seq":1}'),
            (2, b'{"seq":2}'),
        ]
        client = MagicMock()
        client.send_receipts_batch.return_value = _mock_batch_response([
            {"status": "duplicate", "reference_id": "ref-1"},
            {"status": "created", "receipt_id": "bbb", "reference_id": "ref-2"},
        ])
        drainer = BufferDrainer(buffer, client, max_batch_size=50)

        result = drainer._drain_once()

        assert result is True
        buffer.mark_flushed.assert_called_once_with([1, 2])

    def test_all_duplicates_marks_all_flushed(self):
        """Entire batch of duplicates still marks everything flushed."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [
            (1, b'{"seq":1}'),
            (2, b'{"seq":2}'),
            (3, b'{"seq":3}'),
        ]
        client = MagicMock()
        client.send_receipts_batch.return_value = _mock_batch_response([
            {"status": "duplicate", "reference_id": "ref-1"},
            {"status": "duplicate", "reference_id": "ref-2"},
            {"status": "duplicate", "reference_id": "ref-3"},
        ])
        drainer = BufferDrainer(buffer, client, max_batch_size=50)

        result = drainer._drain_once()

        assert result is True
        buffer.mark_flushed.assert_called_once_with([1, 2, 3])

    def test_batch_payloads_passed_correctly(self):
        """Verify the correct payloads (not seqs) are passed to send_receipts_batch."""
        buffer = MagicMock()
        p1, p2 = b'{"seq":1,"tool":"a"}', b'{"seq":2,"tool":"b"}'
        buffer.get_pending.return_value = [(1, p1), (2, p2)]
        client = MagicMock()
        client.send_receipts_batch.return_value = _mock_batch_response([
            {"status": "created", "receipt_id": "a"},
            {"status": "created", "receipt_id": "b"},
        ])
        drainer = BufferDrainer(buffer, client, max_batch_size=50)

        drainer._drain_once()

        client.send_receipts_batch.assert_called_once_with([p1, p2])


class TestBatchDrainPartialFailure:
    def test_partial_failure_flushes_prefix(self):
        """On failed_at_index, only the accepted prefix is flushed."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [
            (1, b'{"seq":1}'),
            (2, b'{"seq":2}'),
            (3, b'{"seq":3}'),
        ]
        client = MagicMock()
        client.send_receipts_batch.return_value = _mock_batch_response(
            results=[
                {"status": "created", "receipt_id": "aaa"},
                {"status": "created", "receipt_id": "bbb"},
                {"status": "error", "error": "bad hmac"},
            ],
            failed_at_index=2,
        )
        drainer = BufferDrainer(buffer, client, max_batch_size=50)

        result = drainer._drain_once()

        assert result is False
        buffer.mark_flushed.assert_called_once_with([1, 2])

    def test_partial_failure_at_index_0_flushes_nothing(self):
        """If the very first receipt fails, nothing is flushed."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [
            (1, b'{"seq":1}'),
            (2, b'{"seq":2}'),
        ]
        client = MagicMock()
        client.send_receipts_batch.return_value = _mock_batch_response(
            results=[
                {"status": "error", "error": "bad schema"},
                {"status": "error", "error": "skipped after failure"},
            ],
            failed_at_index=0,
        )
        drainer = BufferDrainer(buffer, client, max_batch_size=50)

        result = drainer._drain_once()

        assert result is False
        buffer.mark_flushed.assert_not_called()


class TestBatchDrainFallback:
    def test_404_permanently_disables_batch(self):
        """A 404 from the batch endpoint permanently falls back to one-by-one."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [
            (1, b'{"seq":1}'),
            (2, b'{"seq":2}'),
        ]
        client = MagicMock()
        client.send_receipts_batch.side_effect = _http_status_error(404, "Not Found")
        drainer = BufferDrainer(buffer, client, max_batch_size=50)

        drainer._drain_once()

        assert drainer._batch_supported is False
        assert client.send_receipt.call_count == 2

    def test_405_permanently_disables_batch(self):
        """A 405 (Method Not Allowed) also permanently disables batch."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [(1, b'{"seq":1}')]
        client = MagicMock()
        client.send_receipts_batch.side_effect = _http_status_error(405)
        drainer = BufferDrainer(buffer, client, max_batch_size=50)

        drainer._drain_once()

        assert drainer._batch_supported is False
        client.send_receipt.assert_called_once()

    def test_404_subsequent_cycle_stays_one_by_one(self):
        """After 404, the next drain cycle goes directly to one-by-one."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [(1, b'{"seq":1}')]
        client = MagicMock()
        client.send_receipts_batch.side_effect = _http_status_error(404)
        drainer = BufferDrainer(buffer, client, max_batch_size=50)

        drainer._drain_once()
        assert drainer._batch_supported is False

        client.send_receipts_batch.reset_mock()
        client.send_receipt.reset_mock()
        buffer.get_pending.return_value = [(2, b'{"seq":2}')]
        drainer._drain_once()

        client.send_receipts_batch.assert_not_called()
        client.send_receipt.assert_called_once()

    def test_413_falls_back_for_this_cycle_only(self):
        """A 413 falls back to one-by-one but keeps batch enabled."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [(1, b'{"seq":1}')]
        client = MagicMock()
        client.send_receipts_batch.side_effect = _http_status_error(413, "Payload Too Large")
        drainer = BufferDrainer(buffer, client, max_batch_size=50)

        drainer._drain_once()

        assert drainer._batch_supported is True
        client.send_receipt.assert_called_once()

    def test_5xx_retries_and_keeps_batch_enabled(self):
        """A 5xx keeps batch enabled and retries (returns False on exhaustion)."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [(1, b'{"seq":1}')]
        client = MagicMock()
        client.send_receipts_batch.side_effect = _http_status_error(503, "Unavailable")
        drainer = BufferDrainer(buffer, client, max_batch_size=50)

        result = drainer._drain_once()

        assert result is False
        assert drainer._batch_supported is True
        assert client.send_receipts_batch.call_count == 3  # _MAX_RETRIES
        client.send_receipt.assert_not_called()

    def test_response_length_mismatch_falls_back(self):
        """If results array length != chunk length, fall back to one-by-one."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [
            (1, b'{"seq":1}'),
            (2, b'{"seq":2}'),
        ]
        client = MagicMock()
        client.send_receipts_batch.return_value = _mock_batch_response([
            {"status": "created", "receipt_id": "aaa"},
        ])
        drainer = BufferDrainer(buffer, client, max_batch_size=50)

        drainer._drain_once()

        assert drainer._batch_supported is True
        assert client.send_receipt.call_count == 2

    def test_batch_disabled_uses_one_by_one(self):
        """When _batch_supported is False, uses one-by-one path."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [(1, b'{"seq":1}')]
        client = MagicMock()
        drainer = BufferDrainer(buffer, client, max_batch_size=50)
        drainer._batch_supported = False

        drainer._drain_once()

        client.send_receipts_batch.assert_not_called()
        client.send_receipt.assert_called_once()

    def test_max_batch_size_zero_uses_one_by_one(self):
        """max_batch_size=0 effectively disables batch, uses one-by-one."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [(1, b'{"seq":1}')]
        client = MagicMock()
        drainer = BufferDrainer(buffer, client, max_batch_size=0)

        drainer._drain_once()

        client.send_receipts_batch.assert_not_called()
        client.send_receipt.assert_called_once()

    def test_network_error_retries_batch_then_fails(self):
        """A generic network error (not HTTP) retries and gives up."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [(1, b'{"seq":1}')]
        client = MagicMock()
        client.send_receipts_batch.side_effect = httpx.ConnectError("connection refused")
        drainer = BufferDrainer(buffer, client, max_batch_size=50)

        result = drainer._drain_once()

        assert result is False
        assert drainer._batch_supported is True
        assert client.send_receipts_batch.call_count == 3
        client.send_receipt.assert_not_called()


class TestBatchDrainMarkFlushed:
    def test_mark_flushed_failure_in_batch_path_swallowed(self):
        """If mark_flushed raises in the batch path, error is swallowed."""
        buffer = MagicMock()
        buffer.get_pending.return_value = [
            (1, b'{"seq":1}'),
            (2, b'{"seq":2}'),
        ]
        buffer.mark_flushed.side_effect = OSError("sqlite locked")
        client = MagicMock()
        client.send_receipts_batch.return_value = _mock_batch_response([
            {"status": "created", "receipt_id": "aaa"},
            {"status": "created", "receipt_id": "bbb"},
        ])
        drainer = BufferDrainer(buffer, client, max_batch_size=50)

        drainer._drain_once()

        buffer.mark_flushed.assert_called_once_with([1, 2])


class TestBatchDrainEmpty:
    def test_empty_pending_returns_true(self):
        buffer = MagicMock()
        buffer.get_pending.return_value = []
        client = MagicMock()
        drainer = BufferDrainer(buffer, client, max_batch_size=50)

        result = drainer._drain_once()

        assert result is True
        client.send_receipts_batch.assert_not_called()
        client.send_receipt.assert_not_called()
