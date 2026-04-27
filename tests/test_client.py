import httpx
import pytest
import respx

from aevs.config import configure, get_config
from aevs.core.client import AEVSClient
from tests.conftest import TEST_API_KEY


@pytest.fixture
def client() -> AEVSClient:
    configure(api_key=TEST_API_KEY, base_url="https://mock.aevs.io/v1")
    return AEVSClient(get_config())


PAYLOAD = b'{"seq":1,"tool_name":"search"}'


class TestAEVSClient:
    @respx.mock
    def test_send_receipt_success(self, client: AEVSClient):
        route = respx.post("https://mock.aevs.io/v1/receipts").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        response = client.send_receipt(PAYLOAD)
        assert response.status_code == 200
        assert route.called

    @respx.mock
    def test_sends_auth_headers(self, client: AEVSClient):
        route = respx.post("https://mock.aevs.io/v1/receipts").mock(
            return_value=httpx.Response(200)
        )
        client.send_receipt(PAYLOAD)
        request = route.calls.last.request
        assert request.headers["X-AEVS-Key-Id"] == "testkey"
        assert "X-AEVS-Timestamp" in request.headers
        assert "X-AEVS-Signature" in request.headers
        assert request.headers["Content-Type"] == "application/json"

    @respx.mock
    def test_sends_payload_body(self, client: AEVSClient):
        route = respx.post("https://mock.aevs.io/v1/receipts").mock(
            return_value=httpx.Response(200)
        )
        client.send_receipt(PAYLOAD)
        assert route.calls.last.request.content == PAYLOAD

    @respx.mock
    def test_raises_on_5xx(self, client: AEVSClient):
        respx.post("https://mock.aevs.io/v1/receipts").mock(
            return_value=httpx.Response(500)
        )
        with pytest.raises(httpx.HTTPStatusError):
            client.send_receipt(PAYLOAD)

    @respx.mock
    def test_raises_on_401(self, client: AEVSClient):
        respx.post("https://mock.aevs.io/v1/receipts").mock(
            return_value=httpx.Response(401)
        )
        with pytest.raises(httpx.HTTPStatusError):
            client.send_receipt(PAYLOAD)


class TestAEVSClientAsync:
    @respx.mock
    @pytest.mark.asyncio
    async def test_send_receipt_async(self, client: AEVSClient):
        route = respx.post("https://mock.aevs.io/v1/receipts").mock(
            return_value=httpx.Response(200, json={"ok": True})
        )
        response = await client.send_receipt_async(PAYLOAD)
        assert response.status_code == 200
        assert route.called

    @respx.mock
    @pytest.mark.asyncio
    async def test_async_sends_auth_headers(self, client: AEVSClient):
        route = respx.post("https://mock.aevs.io/v1/receipts").mock(
            return_value=httpx.Response(200)
        )
        await client.send_receipt_async(PAYLOAD)
        request = route.calls.last.request
        assert request.headers["X-AEVS-Key-Id"] == "testkey"
        assert "X-AEVS-Signature" in request.headers

    @respx.mock
    @pytest.mark.asyncio
    async def test_async_raises_on_error(self, client: AEVSClient):
        respx.post("https://mock.aevs.io/v1/receipts").mock(
            return_value=httpx.Response(503)
        )
        with pytest.raises(httpx.HTTPStatusError):
            await client.send_receipt_async(PAYLOAD)
