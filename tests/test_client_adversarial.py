"""Adversarial tests for AEVSClient — designed to break it.

Targets uncovered lines: close() with async client created,
aclose() method, async_client lazy property, and cross-loop teardown
paths (issue #3 in PRODUCTION_AUDIT.md).
"""

from __future__ import annotations

import asyncio
import threading

import httpx
import pytest
import respx

from aevs.config import configure, get_config
from aevs.core.client import AEVSClient
from tests.conftest import TEST_API_KEY

PAYLOAD = b'{"seq":1}'


@pytest.fixture
def client() -> AEVSClient:
    configure(api_key=TEST_API_KEY, base_url="https://mock.aevs.io/v1")
    return AEVSClient(get_config())


class TestClientClose:
    def test_close_without_async_client(self, client: AEVSClient):
        """close() when async client was never created should not fail."""
        assert client._async_client is None
        client.close()

    def test_close_with_async_client_created(self, client: AEVSClient):
        """close() must also close the async client if it was lazily created."""
        _ = client.async_client
        assert client._async_client is not None
        client.close()

    def test_close_when_async_close_raises(self, client: AEVSClient):
        """If async_client.close() raises, it must be swallowed."""
        _ = client.async_client
        client._async_client.close = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        client.close()

    @pytest.mark.asyncio
    async def test_aclose(self, client: AEVSClient):
        _ = client.async_client
        await client.aclose()

    @pytest.mark.asyncio
    async def test_aclose_without_async_client(self, client: AEVSClient):
        assert client._async_client is None
        await client.aclose()


class TestAsyncClientLazy:
    def test_async_client_created_on_first_access(self, client: AEVSClient):
        assert client._async_client is None
        ac = client.async_client
        assert ac is not None
        assert isinstance(ac, httpx.AsyncClient)

    def test_async_client_reused_on_second_access(self, client: AEVSClient):
        ac1 = client.async_client
        ac2 = client.async_client
        assert ac1 is ac2
        client.close()


class TestSendReceiptAsync:
    @respx.mock
    @pytest.mark.asyncio
    async def test_async_4xx_raises(self, client: AEVSClient):
        respx.post("https://mock.aevs.io/v1/receipts").mock(
            return_value=httpx.Response(400, text="bad request body")
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.send_receipt_async(PAYLOAD)

    @respx.mock
    @pytest.mark.asyncio
    async def test_async_sends_correct_headers(self, client: AEVSClient):
        route = respx.post("https://mock.aevs.io/v1/receipts").mock(
            return_value=httpx.Response(200)
        )
        await client.send_receipt_async(PAYLOAD)
        request = route.calls.last.request
        assert request.headers["Content-Type"] == "application/json"
        assert "X-AEVS-Key-Id" in request.headers
        assert "X-AEVS-Signature" in request.headers


class TestAsyncLoopTracking:
    """Issue #3: bound-loop must be recorded on first I/O, never before."""

    def test_loop_unset_before_any_io(self, client: AEVSClient):
        _ = client.async_client
        assert client._async_client_loop is None

    @respx.mock
    @pytest.mark.asyncio
    async def test_loop_recorded_after_first_io(self, client: AEVSClient):
        respx.post("https://mock.aevs.io/v1/receipts").mock(
            return_value=httpx.Response(200)
        )
        await client.send_receipt_async(PAYLOAD)
        assert client._async_client_loop is asyncio.get_running_loop()

    @respx.mock
    @pytest.mark.asyncio
    async def test_loop_not_rebound_on_second_io(self, client: AEVSClient):
        """Loop is captured once. Two requests on the same loop must agree."""
        respx.post("https://mock.aevs.io/v1/receipts").mock(
            return_value=httpx.Response(200)
        )
        await client.send_receipt_async(PAYLOAD)
        loop1 = client._async_client_loop
        await client.send_receipt_async(PAYLOAD)
        assert client._async_client_loop is loop1


class TestCloseAcrossLoops:
    """Issue #3: close() must dispatch correctly when the bound loop is
    in another thread, already closed, or never bound at all.

    The previous implementation either fired-and-forgot a never-awaited
    task, or called ``asyncio.run()`` against resources owned by a dead
    loop — both silent footguns in threaded apps and Jupyter.
    """

    def test_close_after_async_use_with_dead_loop(self, client: AEVSClient):
        """Common shutdown path: code did `asyncio.run(...)` with the async
        client, then on cleanup the bound loop is gone.  Must not crash
        and must not attempt to drive a coroutine on the dead loop."""
        respx.start()
        try:
            respx.post("https://mock.aevs.io/v1/receipts").mock(
                return_value=httpx.Response(200)
            )

            async def use_client() -> None:
                await client.send_receipt_async(PAYLOAD)

            asyncio.run(use_client())

            assert client._async_client_loop is not None
            assert client._async_client_loop.is_closed()

            client.close()
            assert client._async_client is None
            assert client._async_client_loop is None
        finally:
            respx.stop()

    @staticmethod
    def _spawn_bg_loop_with_async_client(
        client: AEVSClient,
    ) -> tuple[
        asyncio.AbstractEventLoop, threading.Thread, threading.Event
    ]:
        """Run an event loop in a background thread, perform one request
        on the client (so its connection pool binds to that loop), and
        keep the loop running via ``run_forever``.  Returns the loop, the
        thread, and an Event the caller signals to shut the loop down."""
        loop_ready = threading.Event()
        loop_holder: dict[str, asyncio.AbstractEventLoop] = {}

        def run_loop() -> None:
            loop = asyncio.new_event_loop()
            loop_holder["loop"] = loop
            asyncio.set_event_loop(loop)

            async def first_request() -> None:
                with respx.mock:
                    respx.post("https://mock.aevs.io/v1/receipts").mock(
                        return_value=httpx.Response(200)
                    )
                    await client.send_receipt_async(PAYLOAD)

            loop.run_until_complete(first_request())
            loop_ready.set()
            loop.run_forever()
            loop.close()

        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()
        assert loop_ready.wait(timeout=5.0)
        return loop_holder["loop"], thread, loop_ready

    def test_close_drives_cleanup_on_bound_loop_in_other_thread(
        self, client: AEVSClient
    ):
        """Thread A runs an async loop and uses the client.  The main
        thread calls ``close()``.  Cleanup must execute on loop A via
        ``run_coroutine_threadsafe`` and the AsyncClient must end up
        actually closed (``is_closed`` flips to True)."""
        bound_loop, thread, _ = self._spawn_bg_loop_with_async_client(client)
        try:
            bound = client._async_client
            assert bound is not None
            assert client._async_client_loop is bound_loop
            assert not bound.is_closed

            client.close()
            assert bound.is_closed
        finally:
            bound_loop.call_soon_threadsafe(bound_loop.stop)
            thread.join(timeout=5.0)

    @pytest.mark.asyncio
    async def test_aclose_dispatches_to_bound_loop_in_other_thread(
        self, client: AEVSClient
    ):
        """``aclose()`` from one event loop must dispatch cleanup to the
        bound loop running in a different thread, not call aclose() on
        sockets owned by another loop."""
        bound_loop, thread, _ = self._spawn_bg_loop_with_async_client(client)
        try:
            assert client._async_client_loop is bound_loop
            assert asyncio.get_running_loop() is not bound_loop

            bound = client._async_client
            assert bound is not None
            await client.aclose()
            assert bound.is_closed
        finally:
            bound_loop.call_soon_threadsafe(bound_loop.stop)
            thread.join(timeout=5.0)

    def test_close_when_bound_loop_is_running_in_another_thread_no_io(
        self, client: AEVSClient
    ):
        """Edge case: if `_async_client_loop` is unset (no I/O yet), close()
        runs the unused-client path regardless of any other loops."""
        assert client._async_client_loop is None
        _ = client.async_client
        client.close()
        assert client._async_client is None

    @pytest.mark.asyncio
    @pytest.mark.filterwarnings(
        "ignore:coroutine 'AsyncClient.aclose' was never awaited:RuntimeWarning"
    )
    async def test_aclose_does_not_hang_when_bound_loop_stops_mid_dispatch(
        self, client: AEVSClient
    ):
        """Race: ``bound_loop.is_running()`` says True, then the loop stops
        before ``run_coroutine_threadsafe`` lands.  The scheduled aclose()
        will never run — but ``aclose()`` must not hang forever waiting on
        the future.  It must time out and return.

        Simulated by handing aclose() a fake loop object that claims to be
        running but never executes scheduled coroutines.  The dangling
        ``aclose()`` coroutine is precisely the artefact of the simulated
        race; suppressing the resulting RuntimeWarning is correct.
        """

        class _FakeLoop:
            def is_closed(self) -> bool:
                return False

            def is_running(self) -> bool:
                return True

            def call_soon_threadsafe(self, callback, *args):
                class _Handle:
                    def cancel(self) -> None:
                        return None
                return _Handle()

        client._async_client = client.async_client
        client._async_client_loop = _FakeLoop()  # type: ignore[assignment]

        start = asyncio.get_event_loop().time()
        await client.aclose()
        elapsed = asyncio.get_event_loop().time() - start

        assert elapsed < 7.0, (
            f"aclose() hung for {elapsed:.2f}s — timeout guard regressed"
        )
        assert client._async_client is None
