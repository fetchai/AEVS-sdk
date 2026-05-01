from __future__ import annotations

import asyncio
import logging
import threading

import httpx

from aevs.config import AEVSConfig
from aevs.core.signer import sign_request
from aevs.crypto.hkdf import derive_key

logger = logging.getLogger("aevs")

# Cap response body in WARNING logs — bodies may contain stack traces or PII.
_WARN_BODY_CHARS = 100
_DEBUG_BODY_CHARS = 500

# Generous cap for cross-loop async cleanup; matches drainer-side teardown budgets.
_ACLOSE_TIMEOUT = 5.0


def _log_backend_error(response: httpx.Response) -> None:
    text = response.text
    safe = text[:_WARN_BODY_CHARS] + ("…" if len(text) > _WARN_BODY_CHARS else "")
    ct = response.headers.get("content-type", "")
    logger.warning(
        "AEVS: backend returned %d (content-type=%r, body prefix=%r)",
        response.status_code,
        ct,
        safe,
    )
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "AEVS: backend error body (truncated %d chars): %s",
            _DEBUG_BODY_CHARS,
            text[:_DEBUG_BODY_CHARS],
        )


class AEVSClient:
    """HTTP client for the AEVS backend.

    Handles request signing and provides both sync and async send methods.
    The signing key is derived once at construction to avoid HKDF on every request.
    """

    def __init__(self, config: AEVSConfig) -> None:
        self._config = config
        self._signing_key = derive_key(config.key_secret, salt="aevs-request-v1")
        timeout = config.signing_timeout_ms / 1000.0
        self._client = httpx.Client(
            base_url=config.base_url,
            timeout=timeout,
        )
        self._async_client: httpx.AsyncClient | None = None
        # The event loop the async client's connection pool is bound to.
        # ``None`` until the first ``send_receipt_async`` call performs I/O
        # (httpx/anyio only attach loop-bound resources at that point).
        # Cleanup must be driven on this same loop; see ``close``/``aclose``.
        self._async_client_loop: asyncio.AbstractEventLoop | None = None
        self._async_client_lock = threading.Lock()

    @property
    def async_client(self) -> httpx.AsyncClient:
        """Lazily create the async client (only needed if ainvoke is used).

        Double-checked lock prevents two concurrent coroutines from each
        creating a client and orphaning the first one's connection pool.
        """
        if self._async_client is None:
            with self._async_client_lock:
                if self._async_client is None:
                    timeout = self._config.signing_timeout_ms / 1000.0
                    self._async_client = httpx.AsyncClient(
                        base_url=self._config.base_url,
                        timeout=timeout,
                    )
        return self._async_client

    def send_receipt(self, payload_bytes: bytes) -> httpx.Response:
        """Sign and POST a receipt to the backend. Raises on failure."""
        headers = sign_request(self._config, payload_bytes, signing_key=self._signing_key)
        headers["Content-Type"] = "application/json"
        response = self._client.post("/receipts", content=payload_bytes, headers=headers)
        if response.is_error:
            _log_backend_error(response)
            response.raise_for_status()
        return response

    async def send_receipt_async(self, payload_bytes: bytes) -> httpx.Response:
        """Async version of send_receipt."""
        headers = sign_request(self._config, payload_bytes, signing_key=self._signing_key)
        headers["Content-Type"] = "application/json"
        client = self.async_client
        # Bind to whichever loop performs the first I/O. The connection pool
        # owns sockets on this loop, so cleanup must run there too.
        # Concurrent first-uses on different loops are a misuse pattern;
        # whichever request's write lands first wins, which is enough.
        if self._async_client_loop is None:
            self._async_client_loop = asyncio.get_running_loop()
        response = await client.post(
            "/receipts", content=payload_bytes, headers=headers
        )
        if response.is_error:
            _log_backend_error(response)
            response.raise_for_status()
        return response

    def close(self) -> None:
        """Close the sync HTTP client and best-effort close the async client.

        For deterministic async cleanup, prefer ``await client.aclose()`` from
        within the same event loop that drove ``send_receipt_async``.  ``close``
        handles the common shutdown cases (worker exit, ``atexit`` hooks,
        threaded apps) but cannot guarantee cleanup once the async client's
        bound loop has been torn down — sockets are then released only when
        the OS reaps the process.
        """
        self._client.close()

        async_client = self._async_client
        bound_loop = self._async_client_loop
        self._async_client = None
        self._async_client_loop = None
        if async_client is None:
            return

        self._close_async_client_sync(async_client, bound_loop)

    async def aclose(self) -> None:
        """Close the sync client and the async client on its bound loop.

        Safe to call from any event loop: if the async client was bound to a
        different (still-running) loop, cleanup is dispatched there via
        ``run_coroutine_threadsafe``.  If the bound loop is gone, a warning is
        emitted and cleanup is skipped — there is no safe way to drive a
        coroutine on a dead loop.
        """
        self._client.close()

        async_client = self._async_client
        bound_loop = self._async_client_loop
        self._async_client = None
        self._async_client_loop = None
        if async_client is None:
            return

        running_loop = asyncio.get_running_loop()

        # Never used for I/O, or used on the loop we're already on:
        # safe to await directly.  An unused client's pool is empty, so any
        # loop can drive its no-op aclose().
        if bound_loop is None or bound_loop is running_loop:
            try:
                await async_client.aclose()
            except Exception:
                logger.debug(
                    "AEVS: error during async HTTP client aclose()", exc_info=True
                )
            return

        if bound_loop.is_closed() or not bound_loop.is_running():
            logger.warning(
                "AEVS: async HTTP client's event loop is no longer running; "
                "skipping aclose(). Connections will be released at process exit. "
                "Call `await client.aclose()` while the bound loop is alive to "
                "avoid this."
            )
            return

        # ``is_running()`` may race with the bound loop stopping; bound the
        # await with the same timeout the sync path uses so a stopped loop
        # cannot hang us forever.  ``wait_for`` cancels the wrapped future
        # on timeout, which is fine — we've already cleared local state.
        try:
            future = asyncio.run_coroutine_threadsafe(
                async_client.aclose(), bound_loop
            )
            await asyncio.wait_for(
                asyncio.wrap_future(future), timeout=_ACLOSE_TIMEOUT
            )
        except Exception:
            logger.debug(
                "AEVS: error closing async HTTP client across loops",
                exc_info=True,
            )

    def _close_async_client_sync(
        self,
        async_client: httpx.AsyncClient,
        bound_loop: asyncio.AbstractEventLoop | None,
    ) -> None:
        """Drive ``async_client.aclose()`` from sync code on the right loop.

        Splits the dispatch by bound-loop state instead of blindly calling
        ``loop.create_task`` or ``asyncio.run`` — both of which silently
        operate on the wrong loop in real-world shutdown paths (threaded apps,
        Jupyter, post-``asyncio.run`` cleanup).
        """
        try:
            running_loop: asyncio.AbstractEventLoop | None = (
                asyncio.get_running_loop()
            )
        except RuntimeError:
            running_loop = None

        # Never used for I/O — the connection pool isn't bound anywhere, so
        # any loop can drive its no-op aclose().  We still call it to flip
        # ``is_closed`` and silence httpx's __del__ warning.
        if bound_loop is None:
            if running_loop is None:
                try:
                    asyncio.run(async_client.aclose())
                except Exception:
                    logger.debug(
                        "AEVS: error closing unused async HTTP client",
                        exc_info=True,
                    )
                return
            try:
                running_loop.create_task(async_client.aclose())
            except Exception:
                logger.debug(
                    "AEVS: error scheduling unused async HTTP client close",
                    exc_info=True,
                )
            return

        # Bound loop is gone — we cannot drive a coroutine on a dead loop.
        # Trying ``asyncio.run`` here would close sockets owned by the dead
        # loop, raising "Event loop is closed" or leaking resources.
        if bound_loop.is_closed():
            logger.warning(
                "AEVS: async HTTP client's event loop is already closed; "
                "skipping aclose(). Call `await client.aclose()` before the "
                "loop ends to avoid leaked connections."
            )
            return

        # Called from inside the bound loop, but in sync code: we cannot
        # await.  Best-effort schedule the cleanup and warn the user.
        if running_loop is bound_loop:
            logger.warning(
                "AEVS: close() was called from within the async client's "
                "event loop; cleanup cannot be awaited synchronously. "
                "Use `await client.aclose()` for guaranteed cleanup."
            )
            try:
                bound_loop.create_task(async_client.aclose())
            except Exception:
                logger.debug(
                    "AEVS: error scheduling async close task", exc_info=True
                )
            return

        # Bound loop is alive in another thread — drive cleanup there and
        # block this thread until it completes (or the timeout fires).
        if bound_loop.is_running():
            try:
                future = asyncio.run_coroutine_threadsafe(
                    async_client.aclose(), bound_loop
                )
                future.result(timeout=_ACLOSE_TIMEOUT)
            except Exception:
                logger.debug(
                    "AEVS: error closing async HTTP client across loops",
                    exc_info=True,
                )
            return

        # Bound loop exists but isn't running anywhere; nothing can drive it.
        logger.warning(
            "AEVS: async HTTP client's event loop is not running; "
            "cannot close it cleanly. Connections will be released at "
            "process exit. Call `await client.aclose()` while the loop "
            "is alive to avoid this."
        )
