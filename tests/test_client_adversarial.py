"""Adversarial tests for AEVSClient — designed to break it.

Targets uncovered lines: close() with async client created,
aclose() method, async_client lazy property.
"""

from __future__ import annotations

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
