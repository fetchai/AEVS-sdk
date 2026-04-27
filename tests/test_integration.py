"""Integration tests: full flow from configure -> enable -> tool call -> receipt.

With the buffer-first architecture, tool calls write receipts to the local
buffer instantly.  The background drainer (or an explicit ``flush()``) sends
them to the backend.  Tests that inspect backend traffic therefore call
``aevs.flush()`` after invoking tools.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from langchain_core.tools import tool

import aevs
import aevs._api as _api_mod
from aevs.config import reset_config
from tests.conftest import TEST_API_KEY, TEST_BASE_URL, TEST_RECEIPTS_URL


@tool
def multiply(a: int, b: int) -> int:
    """Multiply two numbers."""
    return a * b


@tool
def broken_tool(x: int) -> int:
    """Always fails."""
    raise ValueError("intentional error")


@pytest.fixture(autouse=True)
def _cleanup(tmp_path):
    """Ensure clean state with an isolated buffer for each test."""
    reset_config()
    yield
    aevs.disable()
    reset_config()


def _configure(tmp_path, **kwargs):
    kwargs.setdefault("base_url", TEST_BASE_URL)
    aevs.configure(
        api_key=TEST_API_KEY,
        buffer_path=str(tmp_path / "buffer.db"),
        **kwargs,
    )


class TestFullFlow:
    @respx.mock
    def test_tool_call_sends_receipt(self, tmp_path):
        route = respx.post(TEST_RECEIPTS_URL).mock(
            return_value=httpx.Response(200, json={"ok": True})
        )

        _configure(tmp_path)
        aevs.enable(frameworks=["langchain"])

        result = multiply.invoke({"a": 6, "b": 7})
        assert result == 42

        assert _api_mod._buffer.pending_count() == 1
        aevs.flush()

        assert route.called
        request = route.calls.last.request
        assert request.headers["X-AEVS-Key-Id"] == "testkey"
        assert "X-AEVS-Signature" in request.headers

        body = json.loads(request.content)
        assert body["tool_name"] == "multiply"
        assert body["status"] == "success"
        assert body["seq"] == 1
        assert "payload_hmac" in body
        assert "prev_hash" in body

    @respx.mock
    def test_error_tool_sends_error_receipt(self, tmp_path):
        route = respx.post(TEST_RECEIPTS_URL).mock(
            return_value=httpx.Response(200)
        )

        _configure(tmp_path)
        aevs.enable(frameworks=["langchain"])

        with pytest.raises(ValueError, match="intentional error"):
            broken_tool.invoke({"x": 1})

        aevs.flush()

        assert route.called
        body = json.loads(route.calls.last.request.content)
        assert body["status"] == "error"
        assert body["error"] == "intentional error"

    @respx.mock
    def test_multiple_calls_increment_seq(self, tmp_path):
        bodies: list[dict] = []

        def capture_request(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200)

        respx.post(TEST_RECEIPTS_URL).mock(side_effect=capture_request)

        _configure(tmp_path)
        aevs.enable(frameworks=["langchain"])

        multiply.invoke({"a": 1, "b": 1})
        multiply.invoke({"a": 2, "b": 2})
        multiply.invoke({"a": 3, "b": 3})

        aevs.flush()

        assert len(bodies) == 3
        assert [b["seq"] for b in bodies] == [1, 2, 3]

    @respx.mock
    def test_chain_links_across_calls(self, tmp_path):
        bodies: list[dict] = []

        def capture(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200)

        respx.post(TEST_RECEIPTS_URL).mock(side_effect=capture)

        _configure(tmp_path)
        aevs.enable(frameworks=["langchain"])

        multiply.invoke({"a": 1, "b": 1})
        multiply.invoke({"a": 2, "b": 2})

        aevs.flush()

        assert bodies[0]["prev_hash"] != bodies[1]["prev_hash"]


class TestBufferFirst:
    """Verify that receipts are always buffered, regardless of backend health."""

    @respx.mock
    def test_receipt_buffered_immediately(self, tmp_path):
        """Tool call writes to buffer even when backend is healthy."""
        respx.post(TEST_RECEIPTS_URL).mock(
            return_value=httpx.Response(200)
        )

        _configure(tmp_path)
        aevs.enable(frameworks=["langchain"])

        multiply.invoke({"a": 3, "b": 4})
        assert _api_mod._buffer is not None
        assert _api_mod._buffer.pending_count() == 1

    @respx.mock
    def test_receipt_buffered_when_backend_down(self, tmp_path):
        respx.post(TEST_RECEIPTS_URL).mock(
            return_value=httpx.Response(500)
        )

        _configure(tmp_path)
        aevs.enable(frameworks=["langchain"])

        result = multiply.invoke({"a": 3, "b": 4})
        assert result == 12
        assert _api_mod._buffer.pending_count() == 1

    @respx.mock
    def test_flush_sends_buffered_receipts(self, tmp_path):
        respx.post(TEST_RECEIPTS_URL).mock(
            return_value=httpx.Response(503)
        )

        _configure(tmp_path)
        aevs.enable(frameworks=["langchain"])

        multiply.invoke({"a": 1, "b": 1})
        multiply.invoke({"a": 2, "b": 2})

        assert _api_mod._buffer.pending_count() == 2

        # Backend comes back
        respx.reset()
        flush_route = respx.post(TEST_RECEIPTS_URL).mock(
            return_value=httpx.Response(200)
        )

        aevs.flush()

        assert flush_route.call_count == 2
        assert _api_mod._buffer.pending_count() == 0

    @respx.mock
    def test_flush_stops_on_failure_and_retries(self, tmp_path):
        """Flush sends in order; persistent backend failure (after retries) stops the cycle."""
        call_count = 0

        def fail_second(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count >= 2 and call_count <= 4:
                return httpx.Response(500)
            return httpx.Response(200)

        respx.post(TEST_RECEIPTS_URL).mock(side_effect=fail_second)

        _configure(tmp_path)
        aevs.enable(frameworks=["langchain"])

        multiply.invoke({"a": 1, "b": 1})
        multiply.invoke({"a": 2, "b": 2})
        multiply.invoke({"a": 3, "b": 3})

        aevs.flush()

        assert _api_mod._buffer.pending_count() == 2  # seq 2 and 3 remain


class TestIdempotency:
    @respx.mock
    def test_enable_twice_no_double_patch(self, tmp_path):
        _configure(tmp_path)
        aevs.enable(frameworks=["langchain"])
        aevs.enable(frameworks=["langchain"])

        multiply.invoke({"a": 1, "b": 1})
        assert _api_mod._buffer.pending_count() == 1

    @respx.mock
    def test_disable_restores(self, tmp_path):
        route = respx.post(TEST_RECEIPTS_URL).mock(
            return_value=httpx.Response(200)
        )

        _configure(tmp_path)
        aevs.enable(frameworks=["langchain"])
        aevs.disable()

        multiply.invoke({"a": 1, "b": 1})
        assert route.call_count == 0

    def test_disable_without_enable(self):
        aevs.disable()


class TestSessionContinuity:
    """Seq and hash chain resume correctly when buffer has data from a prior session."""

    @respx.mock
    def test_seq_resumes_from_buffer(self, tmp_path):
        """Second enable() starts seq after the buffer's max seq."""
        _configure(tmp_path)
        aevs.enable(frameworks=["langchain"])

        multiply.invoke({"a": 1, "b": 1})
        multiply.invoke({"a": 2, "b": 2})

        assert _api_mod._buffer.pending_count() == 2
        assert _api_mod._buffer.max_seq() == 2
        aevs.disable()

        # Session 2: same buffer path
        bodies: list[dict] = []

        def capture(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200)

        respx.post(TEST_RECEIPTS_URL).mock(side_effect=capture)

        _configure(tmp_path)
        aevs.enable(frameworks=["langchain"])

        multiply.invoke({"a": 3, "b": 3})
        aevs.flush()

        assert len(bodies) >= 1
        new_call = [b for b in bodies if b["seq"] == 3]
        assert len(new_call) == 1

    @respx.mock
    def test_hash_chain_continues_across_sessions(self, tmp_path):
        """The prev_hash in session 2 links to the last receipt of session 1."""
        _configure(tmp_path)
        aevs.enable(frameworks=["langchain"])
        multiply.invoke({"a": 1, "b": 1})
        aevs.disable()

        from aevs.core.buffer import LocalBuffer
        from aevs.crypto.chain import compute_receipt_hash
        from tests.conftest import TEST_KEY_SECRET

        buf = LocalBuffer(tmp_path / "buffer.db", TEST_KEY_SECRET)
        last_bytes = buf.last_receipt_bytes()
        buf.close()

        last_hash = compute_receipt_hash(last_bytes)

        # Session 2
        bodies: list[dict] = []

        def capture(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200)

        respx.post(TEST_RECEIPTS_URL).mock(side_effect=capture)

        _configure(tmp_path)
        aevs.enable(frameworks=["langchain"])
        multiply.invoke({"a": 2, "b": 2})
        aevs.flush()

        new_call = [b for b in bodies if b["seq"] == 2]
        assert len(new_call) == 1
        assert new_call[0]["prev_hash"] == last_hash

    @respx.mock
    def test_fresh_buffer_starts_at_seq_1(self, tmp_path):
        """With no prior buffer, seq starts at 1 as before."""
        bodies: list[dict] = []

        def capture(request: httpx.Request) -> httpx.Response:
            bodies.append(json.loads(request.content))
            return httpx.Response(200)

        respx.post(TEST_RECEIPTS_URL).mock(side_effect=capture)

        _configure(tmp_path)
        aevs.enable(frameworks=["langchain"])
        multiply.invoke({"a": 1, "b": 1})
        aevs.flush()

        assert bodies[0]["seq"] == 1


class TestNeverBreakAgent:
    @respx.mock
    def test_connection_error_still_returns(self, tmp_path):
        respx.post(TEST_RECEIPTS_URL).mock(
            side_effect=httpx.ConnectError("connection refused")
        )

        _configure(tmp_path)
        aevs.enable(frameworks=["langchain"])

        result = multiply.invoke({"a": 5, "b": 5})
        assert result == 25

    @respx.mock
    def test_serialization_error_does_not_crash_agent(self, tmp_path):
        """If receipt building or serialization fails, the tool still returns."""
        respx.post(TEST_RECEIPTS_URL).mock(
            return_value=httpx.Response(200)
        )

        _configure(tmp_path)
        aevs.enable(frameworks=["langchain"])

        _api_mod._receipt_builder._payload_key = b"bad"
        _api_mod._receipt_builder._config = None

        result = multiply.invoke({"a": 7, "b": 3})
        assert result == 21
