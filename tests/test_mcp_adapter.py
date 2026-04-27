from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aevs.adapters.base import _aevs_tracking_active
from aevs.adapters.mcp import (
    MCPAdapter,
    _check_mcp_version,
    _extract_error_text,
    _is_task_result,
    _serialize_call_tool_result,
    _serialize_content_block,
)

# ---------------------------------------------------------------------------
# Lightweight MCP type stubs
# ---------------------------------------------------------------------------


def _make_text_content(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_image_content(data: str, mime: str = "image/png") -> MagicMock:
    block = MagicMock()
    block.type = "image"
    block.data = data
    block.mimeType = mime
    return block


def _make_audio_content(data: str, mime: str = "audio/wav") -> MagicMock:
    block = MagicMock()
    block.type = "audio"
    block.data = data
    block.mimeType = mime
    return block


def _make_resource_content(uri: str) -> MagicMock:
    resource = MagicMock()
    resource.uri = uri
    block = MagicMock()
    block.type = "resource"
    block.resource = resource
    return block


def _make_resource_link_content(uri: str) -> MagicMock:
    block = MagicMock()
    block.type = "resource_link"
    block.uri = uri
    return block


def _make_call_tool_result(
    content: list[Any] | None = None,
    is_error: bool = False,
    structured_content: dict[str, Any] | None = None,
) -> MagicMock:
    result = MagicMock()
    result.content = content if content is not None else []
    result.isError = is_error
    result.structuredContent = structured_content
    return result


def _make_create_task_result() -> Any:
    """Build a stub whose class name is ``CreateTaskResult``."""
    cls = type("CreateTaskResult", (), {})
    return cls()


# ---------------------------------------------------------------------------
# Fixture: patch/unpatch around the real ClientSession.call_tool so
# tests don't leak state to each other across the class-level method.
# ---------------------------------------------------------------------------


@pytest.fixture()
def _restore_call_tool():
    """Save and restore ClientSession.call_tool around each test."""
    from mcp.client.session import ClientSession

    original = ClientSession.call_tool
    yield
    ClientSession.call_tool = original


# ---------------------------------------------------------------------------
# _serialize_content_block
# ---------------------------------------------------------------------------


class TestSerializeContentBlock:
    def test_text_content(self):
        block = _make_text_content("hello world")
        out = _serialize_content_block(block)
        assert out == {"type": "text", "text": "hello world"}

    def test_image_content_hashed(self):
        raw_b64 = "iVBORw0KGgoAAAANSUhEUgAAAAUA"
        block = _make_image_content(raw_b64, "image/png")
        out = _serialize_content_block(block)

        assert out["type"] == "image"
        assert out["mimeType"] == "image/png"
        decoded = base64.b64decode(raw_b64)
        expected_hash = hashlib.sha256(decoded).hexdigest()
        assert out["_aevs_data_sha256"] == expected_hash
        assert out["_aevs_data_bytes"] == len(decoded)
        assert "data" not in out

    def test_audio_content_hashed(self):
        raw_data = "UklGRiQAAABXQVZFZm10"
        block = _make_audio_content(raw_data)
        out = _serialize_content_block(block)

        assert out["type"] == "audio"
        assert out["mimeType"] == "audio/wav"
        decoded = base64.b64decode(raw_data)
        assert out["_aevs_data_sha256"] == hashlib.sha256(decoded).hexdigest()
        assert out["_aevs_data_bytes"] == len(decoded)
        assert "data" not in out

    def test_image_content_invalid_base64_falls_back(self):
        bad_data = "not-valid-b64!!"
        block = _make_image_content(bad_data, "image/jpeg")
        out = _serialize_content_block(block)

        assert out["type"] == "image"
        assert out["_aevs_data_sha256"] == hashlib.sha256(bad_data.encode("utf-8")).hexdigest()
        assert out["_aevs_data_bytes"] == len(bad_data.encode("utf-8"))

    def test_resource_content(self):
        block = _make_resource_content("file:///tmp/data.csv")
        out = _serialize_content_block(block)
        assert out == {"type": "resource", "uri": "file:///tmp/data.csv"}

    def test_resource_link_content(self):
        block = _make_resource_link_content("https://example.com/doc.pdf")
        out = _serialize_content_block(block)
        assert out == {"type": "resource_link", "uri": "https://example.com/doc.pdf"}

    def test_unknown_content_type(self):
        block = MagicMock()
        block.type = "video"
        out = _serialize_content_block(block)
        assert out["type"] == "video"
        assert "_raw" in out

    def test_unknown_content_type_truncation(self):
        block = MagicMock()
        block.type = "huge"
        block.__str__ = lambda self: "X" * 1000
        out = _serialize_content_block(block)
        assert len(out["_raw"]) <= 500

    def test_image_content_none_data(self):
        block = MagicMock()
        block.type = "image"
        block.data = None
        block.mimeType = "image/png"
        out = _serialize_content_block(block)
        assert out["type"] == "image"
        assert out["_aevs_data_bytes"] == 0


# ---------------------------------------------------------------------------
# _serialize_call_tool_result
# ---------------------------------------------------------------------------


class TestSerializeCallToolResult:
    def test_structured_content_preferred(self):
        result = _make_call_tool_result(
            content=[_make_text_content("fallback")],
            structured_content={"key": "value", "count": 42},
        )
        out = _serialize_call_tool_result(result)
        assert out == {"structured": {"key": "value", "count": 42}}

    def test_single_text_unwrapped(self):
        result = _make_call_tool_result(content=[_make_text_content("just text")])
        out = _serialize_call_tool_result(result)
        assert out == "just text"

    def test_multiple_content_blocks(self):
        result = _make_call_tool_result(
            content=[_make_text_content("caption"), _make_image_content("AAAA")]
        )
        out = _serialize_call_tool_result(result)
        assert isinstance(out, dict)
        assert "content" in out
        assert len(out["content"]) == 2
        assert out["content"][0]["type"] == "text"
        assert out["content"][1]["type"] == "image"

    def test_none_content(self):
        result = MagicMock()
        result.structuredContent = None
        result.content = None
        assert _serialize_call_tool_result(result) is None

    def test_empty_content_list(self):
        result = _make_call_tool_result(content=[])
        out = _serialize_call_tool_result(result)
        assert out == {"content": []}

    def test_empty_structured_content(self):
        result = _make_call_tool_result(structured_content={})
        out = _serialize_call_tool_result(result)
        assert out == {"structured": {}}


# ---------------------------------------------------------------------------
# _extract_error_text
# ---------------------------------------------------------------------------


class TestExtractErrorText:
    def test_extracts_first_text(self):
        result = _make_call_tool_result(
            content=[_make_text_content("something went wrong")],
            is_error=True,
        )
        assert _extract_error_text(result) == "something went wrong"

    def test_fallback_when_no_text(self):
        result = _make_call_tool_result(content=[], is_error=True)
        assert _extract_error_text(result) == "MCP tool returned isError=True"

    def test_skips_none_text_block(self):
        block = MagicMock()
        block.type = "text"
        block.text = None
        result = _make_call_tool_result(content=[block], is_error=True)
        assert _extract_error_text(result) == "MCP tool returned isError=True"

    def test_extracts_text_after_non_text_block(self):
        img_block = _make_image_content("AAAA")
        txt_block = _make_text_content("real error")
        result = _make_call_tool_result(content=[img_block, txt_block], is_error=True)
        assert _extract_error_text(result) == "real error"


# ---------------------------------------------------------------------------
# _is_task_result
# ---------------------------------------------------------------------------


class TestIsTaskResult:
    def test_detects_create_task_result(self):
        assert _is_task_result(_make_create_task_result()) is True

    def test_rejects_normal_result(self):
        assert _is_task_result(_make_call_tool_result()) is False


# ---------------------------------------------------------------------------
# _check_mcp_version
# ---------------------------------------------------------------------------


class TestCheckMcpVersion:
    def test_valid_version(self):
        with patch("importlib.metadata.version", return_value="1.27.0"):
            assert _check_mcp_version() is True

    def test_minimum_version(self):
        with patch("importlib.metadata.version", return_value="1.20.0"):
            assert _check_mcp_version() is True

    def test_old_version(self):
        with patch("importlib.metadata.version", return_value="1.19.5"):
            assert _check_mcp_version() is False

    def test_major_2(self):
        with patch("importlib.metadata.version", return_value="2.0.0"):
            assert _check_mcp_version() is True

    def test_pre_release_version(self):
        with patch("importlib.metadata.version", return_value="1.22a1"):
            assert _check_mcp_version() is True

    def test_pre_release_old(self):
        with patch("importlib.metadata.version", return_value="1.19rc2"):
            assert _check_mcp_version() is False

    def test_import_error(self):
        with patch("importlib.metadata.version", side_effect=Exception("not installed")):
            assert _check_mcp_version() is False


# ---------------------------------------------------------------------------
# MCPAdapter core
# ---------------------------------------------------------------------------


class TestMCPAdapterBasics:
    def setup_method(self):
        self.adapter = MCPAdapter()

    def test_name(self):
        assert self.adapter.name == "mcp"

    def test_is_available(self):
        assert self.adapter.is_available() is True

    def test_is_available_without_mcp(self):
        with patch.dict("sys.modules", {"mcp.client.session": None}):
            adapter = MCPAdapter()
            assert adapter.is_available() is False


# ---------------------------------------------------------------------------
# MCPAdapter patch / unpatch / intercept
#
# Strategy: before the adapter patches, we replace ClientSession.call_tool
# with an AsyncMock.  The adapter saves THAT as the "original".  Tests then
# call the patched method, which internally awaits the AsyncMock.
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_restore_call_tool")
class TestMCPAdapterPatch:
    def setup_method(self):
        self.async_calls: list[dict] = []
        self.adapter = MCPAdapter()

        from mcp.client.session import ClientSession

        self.fake_result = _make_call_tool_result(
            content=[_make_text_content("ok")]
        )
        self.mock_original = AsyncMock(return_value=self.fake_result)
        ClientSession.call_tool = self.mock_original  # type: ignore[assignment]

    def _sync_handler(self, **kwargs: Any) -> None:
        pass

    async def _async_handler(self, **kwargs: Any) -> None:
        self.async_calls.append(kwargs)

    def teardown_method(self):
        if hasattr(self, "adapter"):
            self.adapter.unpatch()

    @pytest.mark.asyncio
    async def test_intercepts_successful_call(self):
        from mcp.client.session import ClientSession

        self.adapter.patch(self._sync_handler, self._async_handler)

        session = MagicMock(spec=ClientSession)
        result = await ClientSession.call_tool(
            session, "get_price", {"coin": "bitcoin"}
        )

        assert result is self.fake_result
        assert len(self.async_calls) == 1
        call = self.async_calls[0]
        assert call["tool_name"] == "get_price"
        assert call["inputs"] == {"coin": "bitcoin"}
        assert call["status"] == "success"
        assert call["error"] is None
        assert call["framework"] == "mcp"
        assert isinstance(call["started_at"], datetime)
        assert isinstance(call["ended_at"], datetime)
        assert call["tool_call_id"] is not None
        assert call["parent_run_id"] is None

    @pytest.mark.asyncio
    async def test_intercepts_error_result(self):
        from mcp.client.session import ClientSession

        error_result = _make_call_tool_result(
            content=[_make_text_content("not found")],
            is_error=True,
        )
        self.mock_original.return_value = error_result
        self.adapter.patch(self._sync_handler, self._async_handler)

        session = MagicMock(spec=ClientSession)
        result = await ClientSession.call_tool(session, "missing", {"id": 99})

        assert result is error_result
        assert len(self.async_calls) == 1
        assert self.async_calls[0]["status"] == "error"
        assert self.async_calls[0]["error"] == "not found"

    @pytest.mark.asyncio
    async def test_arguments_none_normalised(self):
        from mcp.client.session import ClientSession

        self.adapter.patch(self._sync_handler, self._async_handler)

        session = MagicMock(spec=ClientSession)
        await ClientSession.call_tool(session, "no_args_tool", None)

        assert len(self.async_calls) == 1
        assert self.async_calls[0]["inputs"] == {}

    @pytest.mark.asyncio
    async def test_handler_failure_does_not_break_tool(self):
        from mcp.client.session import ClientSession

        handler_invoked = False

        async def broken_handler(**kwargs: Any) -> None:
            nonlocal handler_invoked
            handler_invoked = True
            raise RuntimeError("handler exploded")

        self.adapter.patch(self._sync_handler, broken_handler)

        session = MagicMock(spec=ClientSession)
        result = await ClientSession.call_tool(session, "safe_tool", {"x": 1})

        assert result is self.fake_result
        assert handler_invoked, "handler should have been called even though it raised"

    @pytest.mark.asyncio
    async def test_serialization_failure_does_not_break_tool(self):
        """If _serialize_call_tool_result raises, the user still gets the result."""
        from mcp.client.session import ClientSession

        bad_result = MagicMock()
        bad_result.isError = False
        bad_result.structuredContent = None
        bad_result.content = object()  # not iterable → raises in serialiser
        self.mock_original.return_value = bad_result
        self.adapter.patch(self._sync_handler, self._async_handler)

        session = MagicMock(spec=ClientSession)
        result = await ClientSession.call_tool(session, "tricky", {})

        assert result is bad_result

    @pytest.mark.asyncio
    async def test_exception_preserved(self):
        from mcp.client.session import ClientSession

        self.mock_original.side_effect = ConnectionError("server down")
        self.adapter.patch(self._sync_handler, self._async_handler)

        session = MagicMock(spec=ClientSession)
        with pytest.raises(ConnectionError, match="server down"):
            await ClientSession.call_tool(session, "broken", {})

        assert len(self.async_calls) == 1
        assert self.async_calls[0]["status"] == "error"
        assert self.async_calls[0]["error"] == "server down"

    def test_idempotent_patch(self):
        self.adapter.patch(self._sync_handler, self._async_handler)
        first_original = self.adapter._original_call_tool
        self.adapter.patch(self._sync_handler, self._async_handler)
        assert self.adapter._original_call_tool is first_original

    def test_unpatch_restores(self):
        from mcp.client.session import ClientSession

        original_fn = ClientSession.call_tool
        self.adapter.patch(self._sync_handler, self._async_handler)
        assert ClientSession.call_tool is not original_fn

        self.adapter.unpatch()
        assert ClientSession.call_tool is original_fn

    def test_idempotent_unpatch(self):
        self.adapter.unpatch()
        self.adapter.patch(self._sync_handler, self._async_handler)
        self.adapter.unpatch()
        self.adapter.unpatch()

    @pytest.mark.asyncio
    async def test_meta_run_id_extraction(self):
        from mcp.client.session import ClientSession

        self.adapter.patch(self._sync_handler, self._async_handler)

        session = MagicMock(spec=ClientSession)
        await ClientSession.call_tool(
            session,
            "tracked_tool",
            {"arg": 1},
            meta={"run_id": "run-abc-123", "parent_run_id": "parent-xyz"},
        )

        assert len(self.async_calls) == 1
        assert self.async_calls[0]["run_id"] == "run-abc-123"
        assert self.async_calls[0]["parent_run_id"] == "parent-xyz"

    @pytest.mark.asyncio
    async def test_meta_trace_id_fallback(self):
        from mcp.client.session import ClientSession

        self.adapter.patch(self._sync_handler, self._async_handler)

        session = MagicMock(spec=ClientSession)
        await ClientSession.call_tool(
            session,
            "traced_tool",
            {},
            meta={"trace_id": "trace-999"},
        )

        assert self.async_calls[0]["run_id"] == "trace-999"

    @pytest.mark.asyncio
    async def test_framework_field(self):
        from importlib.metadata import version

        from mcp.client.session import ClientSession

        self.adapter.patch(self._sync_handler, self._async_handler)

        session = MagicMock(spec=ClientSession)
        await ClientSession.call_tool(session, "any_tool", {})

        call = self.async_calls[0]
        assert call["framework"] == "mcp"
        assert call["framework_version"] == version("mcp")

    @pytest.mark.asyncio
    async def test_synthetic_tool_call_id(self):
        from mcp.client.session import ClientSession

        self.adapter.patch(self._sync_handler, self._async_handler)

        session = MagicMock(spec=ClientSession)
        await ClientSession.call_tool(session, "tool_a", {})
        await ClientSession.call_tool(session, "tool_b", {})

        id_a = self.async_calls[0]["tool_call_id"]
        id_b = self.async_calls[1]["tool_call_id"]
        assert id_a is not None
        assert id_b is not None
        assert id_a != id_b

    @pytest.mark.asyncio
    async def test_structured_content_in_output(self):
        from mcp.client.session import ClientSession

        structured_result = _make_call_tool_result(
            content=[_make_text_content("fallback")],
            structured_content={"price": 50000, "currency": "USD"},
        )
        self.mock_original.return_value = structured_result
        self.adapter.patch(self._sync_handler, self._async_handler)

        session = MagicMock(spec=ClientSession)
        await ClientSession.call_tool(session, "get_price", {"coin": "btc"})

        output = self.async_calls[0]["output"]
        assert output == {"structured": {"price": 50000, "currency": "USD"}}

    @pytest.mark.asyncio
    async def test_binary_content_hashed_in_output(self):
        from mcp.client.session import ClientSession

        img_result = _make_call_tool_result(
            content=[_make_text_content("chart"), _make_image_content("AAAA", "image/png")],
        )
        self.mock_original.return_value = img_result
        self.adapter.patch(self._sync_handler, self._async_handler)

        session = MagicMock(spec=ClientSession)
        await ClientSession.call_tool(session, "chart_tool", {})

        output = self.async_calls[0]["output"]
        assert "content" in output
        assert output["content"][1]["type"] == "image"
        assert "_aevs_data_sha256" in output["content"][1]
        assert "data" not in output["content"][1]

    @pytest.mark.asyncio
    async def test_create_task_result_skipped(self):
        """CreateTaskResult should be returned as-is with no receipt created."""
        from mcp.client.session import ClientSession

        task_result = _make_create_task_result()
        self.mock_original.return_value = task_result
        self.adapter.patch(self._sync_handler, self._async_handler)

        session = MagicMock(spec=ClientSession)
        result = await ClientSession.call_tool(session, "long_running", {})

        assert result is task_result
        assert len(self.async_calls) == 0, "no receipt should be emitted for CreateTaskResult"


# ---------------------------------------------------------------------------
# Cross-adapter deduplication via _aevs_tracking_active
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("_restore_call_tool")
class TestCrossAdapterDedup:
    def setup_method(self):
        self.mcp_calls: list[dict] = []
        self.adapter = MCPAdapter()

        from mcp.client.session import ClientSession

        self.fake_result = _make_call_tool_result(content=[_make_text_content("ok")])
        self.mock_original = AsyncMock(return_value=self.fake_result)
        ClientSession.call_tool = self.mock_original  # type: ignore[assignment]

    async def _async_handler(self, **kwargs: Any) -> None:
        self.mcp_calls.append(kwargs)

    def _sync_handler(self, **kwargs: Any) -> None:
        pass

    def teardown_method(self):
        if hasattr(self, "adapter"):
            self.adapter.unpatch()

    @pytest.mark.asyncio
    async def test_skips_when_tracking_active(self):
        """When _aevs_tracking_active is already set (by e.g. LangChain
        adapter), the MCP adapter should forward the call without creating
        a receipt."""
        from mcp.client.session import ClientSession

        self.adapter.patch(self._sync_handler, self._async_handler)

        session = MagicMock(spec=ClientSession)

        token = _aevs_tracking_active.set(True)
        try:
            result = await ClientSession.call_tool(session, "deduped_tool", {"a": 1})
        finally:
            _aevs_tracking_active.reset(token)

        assert result is self.fake_result
        assert len(self.mcp_calls) == 0

    @pytest.mark.asyncio
    async def test_fires_when_tracking_not_active(self):
        """When no other adapter is tracking, MCP adapter should fire."""
        from mcp.client.session import ClientSession

        self.adapter.patch(self._sync_handler, self._async_handler)

        assert _aevs_tracking_active.get(False) is False

        session = MagicMock(spec=ClientSession)
        await ClientSession.call_tool(session, "solo_tool", {"b": 2})

        assert len(self.mcp_calls) == 1
        assert self.mcp_calls[0]["tool_name"] == "solo_tool"

    @pytest.mark.asyncio
    async def test_tracking_cleared_after_call(self):
        """The tracking flag should be reset after the call completes,
        so subsequent independent calls are still intercepted."""
        from mcp.client.session import ClientSession

        self.adapter.patch(self._sync_handler, self._async_handler)

        session = MagicMock(spec=ClientSession)
        await ClientSession.call_tool(session, "first_call", {})
        await ClientSession.call_tool(session, "second_call", {})

        assert len(self.mcp_calls) == 2
        assert _aevs_tracking_active.get(False) is False

    @pytest.mark.asyncio
    async def test_tracking_cleared_on_exception(self):
        """The tracking flag must be reset even if the original call raises."""
        from mcp.client.session import ClientSession

        self.mock_original.side_effect = RuntimeError("boom")
        self.adapter.patch(self._sync_handler, self._async_handler)

        session = MagicMock(spec=ClientSession)
        with pytest.raises(RuntimeError, match="boom"):
            await ClientSession.call_tool(session, "exploding", {})

        assert _aevs_tracking_active.get(False) is False

    @pytest.mark.asyncio
    async def test_concurrent_async_calls_independent(self):
        """Two concurrent tasks via asyncio.gather should each produce an
        independent receipt with distinct tool_call_ids."""
        from mcp.client.session import ClientSession

        self.adapter.patch(self._sync_handler, self._async_handler)
        session = MagicMock(spec=ClientSession)

        async def call(tool_name: str) -> Any:
            return await ClientSession.call_tool(session, tool_name, {})

        r1, r2 = await asyncio.gather(call("tool_x"), call("tool_y"))

        assert r1 is self.fake_result
        assert r2 is self.fake_result
        assert len(self.mcp_calls) == 2
        ids = {c["tool_call_id"] for c in self.mcp_calls}
        assert len(ids) == 2, "each concurrent call must get a unique tool_call_id"
        names = {c["tool_name"] for c in self.mcp_calls}
        assert names == {"tool_x", "tool_y"}


# ---------------------------------------------------------------------------
# LangChain adapter _aevs_tracking_active guard
# ---------------------------------------------------------------------------

try:
    from aevs.adapters.langchain import LangChainAdapter as _LangChainAdapter

    _HAS_LANGCHAIN = True
except ImportError:
    _HAS_LANGCHAIN = False


@pytest.mark.skipif(not _HAS_LANGCHAIN, reason="langchain-core not installed")
class TestLangChainDedupGuard:
    """Verify the LangChain adapter also skips receipt creation when
    _aevs_tracking_active is already set (the mirror of the MCP-side
    test in TestCrossAdapterDedup)."""

    def setup_method(self):
        self.calls: list[dict] = []
        self.adapter = _LangChainAdapter()

    def _sync_handler(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    async def _async_handler(self, **kwargs: Any) -> None:
        pass

    def teardown_method(self):
        self.adapter.unpatch()

    def test_langchain_skips_when_tracking_active(self):
        from langchain_core.tools import tool

        @tool
        def add(a: int, b: int) -> int:
            """Add two numbers."""
            return a + b

        self.adapter.patch(self._sync_handler, self._async_handler)

        token = _aevs_tracking_active.set(True)
        try:
            result = add.invoke({"a": 3, "b": 4})
        finally:
            _aevs_tracking_active.reset(token)

        assert result == 7
        assert len(self.calls) == 0, "no receipt when tracking already active"

    def test_langchain_fires_when_tracking_not_active(self):
        from langchain_core.tools import tool

        @tool
        def mul(a: int, b: int) -> int:
            """Multiply two numbers."""
            return a * b

        self.adapter.patch(self._sync_handler, self._async_handler)

        assert _aevs_tracking_active.get(False) is False
        result = mul.invoke({"a": 3, "b": 4})

        assert result == 12
        assert len(self.calls) == 1


# ---------------------------------------------------------------------------
# _warn_dual_mcp_langchain
# ---------------------------------------------------------------------------


class TestWarnDualMcpLangchain:
    def test_warns_when_both_adapters_and_bridge_present(self):
        from aevs._api import _warn_dual_mcp_langchain

        mcp_adapter = MagicMock()
        mcp_adapter.name = "mcp"
        lc_adapter = MagicMock()
        lc_adapter.name = "langchain"

        with patch.dict("sys.modules", {"langchain_mcp_adapters": MagicMock()}):
            with patch("aevs._api.logger") as mock_logger:
                _warn_dual_mcp_langchain([mcp_adapter, lc_adapter])
                mock_logger.warning.assert_called_once()

    def test_no_warn_single_adapter(self):
        from aevs._api import _warn_dual_mcp_langchain

        mcp_adapter = MagicMock()
        mcp_adapter.name = "mcp"

        with patch("aevs._api.logger") as mock_logger:
            _warn_dual_mcp_langchain([mcp_adapter])
            mock_logger.warning.assert_not_called()

    def test_no_warn_without_bridge(self):
        from aevs._api import _warn_dual_mcp_langchain

        mcp_adapter = MagicMock()
        mcp_adapter.name = "mcp"
        lc_adapter = MagicMock()
        lc_adapter.name = "langchain"

        with patch.dict("sys.modules", {"langchain_mcp_adapters": None}):
            with patch("aevs._api.logger") as mock_logger:
                _warn_dual_mcp_langchain([mcp_adapter, lc_adapter])
                mock_logger.warning.assert_not_called()
