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
        response = await self.async_client.post(
            "/receipts", content=payload_bytes, headers=headers
        )
        if response.is_error:
            _log_backend_error(response)
            response.raise_for_status()
        return response

    def close(self) -> None:
        self._client.close()
        if self._async_client is not None:
            async_client = self._async_client
            self._async_client = None
            try:
                loop = asyncio.get_running_loop()
                # Called from within a running async context — schedule cleanup as a fire-and-forget
                # task so we don't block the caller or create a new loop.
                loop.create_task(async_client.aclose())
            except RuntimeError:
                # No running event loop — safe to drive it synchronously.
                asyncio.run(async_client.aclose())
            except Exception:
                logger.debug("AEVS: error closing async HTTP client", exc_info=True)

    async def aclose(self) -> None:
        self._client.close()
        if self._async_client is not None:
            async_client = self._async_client
            self._async_client = None
            await async_client.aclose()
