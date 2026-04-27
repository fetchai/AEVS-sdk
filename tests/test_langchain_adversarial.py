"""Adversarial tests for the LangChain adapter — designed to break it.

Targets uncovered lines: _get_langchain_version exception path,
_extract_parent_run_id edge cases, _inject_capture with add_handler,
patched_ainvoke async path (lines 171-218), and RunIdCapture
ignore_* properties (lines 89, 93, 97).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.tools import BaseTool

from aevs.adapters.langchain import (
    LangChainAdapter,
    _extract_parent_run_id,
    _get_langchain_version,
    _inject_capture,
)


class TestGetLangchainVersion:
    def test_returns_actual_version(self):
        ver = _get_langchain_version()
        assert ver != "unknown"
        assert "." in ver

    def test_returns_unknown_on_exception(self):
        with patch("importlib.metadata.version", side_effect=Exception("no pkg")):
            assert _get_langchain_version() == "unknown"


class TestExtractParentRunId:
    def test_returns_none_for_non_dict(self):
        assert _extract_parent_run_id(None) is None
        assert _extract_parent_run_id("string") is None
        assert _extract_parent_run_id(42) is None

    def test_returns_none_when_no_callbacks(self):
        assert _extract_parent_run_id({}) is None
        assert _extract_parent_run_id({"other": "stuff"}) is None

    def test_returns_none_when_callbacks_has_no_parent_run_id(self):
        assert _extract_parent_run_id({"callbacks": [1, 2]}) is None

    def test_extracts_parent_run_id_from_callback_manager(self):
        cbs = MagicMock()
        cbs.parent_run_id = "parent-123"
        result = _extract_parent_run_id({"callbacks": cbs})
        assert result == "parent-123"

    def test_returns_none_when_parent_run_id_is_none(self):
        cbs = MagicMock()
        cbs.parent_run_id = None
        result = _extract_parent_run_id({"callbacks": cbs})
        assert result is None


class TestInjectCapture:
    def test_creates_list_when_callbacks_missing(self):
        config: dict[str, Any] = {}
        capture = MagicMock()
        _inject_capture(config, capture)
        assert config["callbacks"] == [capture]

    def test_appends_to_existing_list(self):
        existing = [MagicMock()]
        config: dict[str, Any] = {"callbacks": existing}
        capture = MagicMock()
        _inject_capture(config, capture)
        assert len(config["callbacks"]) == 2
        assert config["callbacks"][-1] is capture

    def test_calls_add_handler_on_manager(self):
        manager = MagicMock()
        config: dict[str, Any] = {"callbacks": manager}
        capture = MagicMock()
        _inject_capture(config, capture)
        manager.add_handler.assert_called_once_with(capture)

    def test_noop_when_callbacks_is_unexpected_type(self):
        config: dict[str, Any] = {"callbacks": 42}
        capture = MagicMock()
        _inject_capture(config, capture)
        assert config["callbacks"] == 42


class _NativeAsyncTool(BaseTool):
    """A tool with native async _arun to force the ainvoke async path."""
    name: str = "native_async_tool"
    description: str = "Native async tool for testing"

    def _run(self, a: int = 0, b: int = 0) -> int:
        return a + b

    async def _arun(self, a: int = 0, b: int = 0) -> int:
        return a + b


class _NativeAsyncFailTool(BaseTool):
    """A native async tool that always fails."""
    name: str = "native_async_fail"
    description: str = "Always fails async"

    def _run(self, msg: str = "err") -> str:
        raise ValueError(msg)

    async def _arun(self, msg: str = "err") -> str:
        raise ValueError(msg)


class TestLangChainAdapterAsyncAinvokePath:
    """Exercise the actual patched_ainvoke code (lines 171-218).

    StructuredTool.ainvoke delegates to invoke via run_in_executor,
    so to hit the actual ainvoke monkey-patch we need a tool with
    a native _arun implementation.
    """

    def setup_method(self):
        self.sync_calls: list[dict] = []
        self.async_calls: list[dict] = []
        self.adapter = LangChainAdapter()

    def _sync_handler(self, **kwargs: Any) -> None:
        self.sync_calls.append(kwargs)

    async def _async_handler(self, **kwargs: Any) -> None:
        self.async_calls.append(kwargs)

    def teardown_method(self):
        self.adapter.unpatch()

    @pytest.mark.asyncio
    async def test_ainvoke_native_async_uses_async_handler(self):
        tool = _NativeAsyncTool()
        self.adapter.patch(self._sync_handler, self._async_handler)
        result = await tool.ainvoke({"a": 10, "b": 5})
        assert result == 15

        all_calls = self.sync_calls + self.async_calls
        assert len(all_calls) >= 1
        call = all_calls[0]
        assert call["tool_name"] == "native_async_tool"
        assert call["framework"] == "langchain"
        assert call["status"] == "success"
        assert isinstance(call["started_at"], datetime)
        assert isinstance(call["ended_at"], datetime)

    @pytest.mark.asyncio
    async def test_ainvoke_native_async_preserves_exception(self):
        tool = _NativeAsyncFailTool()
        self.adapter.patch(self._sync_handler, self._async_handler)
        with pytest.raises(ValueError, match="kaboom"):
            await tool.ainvoke({"msg": "kaboom"})

        all_calls = self.sync_calls + self.async_calls
        assert len(all_calls) >= 1
        assert all_calls[0]["status"] == "error"
        assert all_calls[0]["error"] == "kaboom"

    @pytest.mark.asyncio
    async def test_ainvoke_async_handler_crash_does_not_break_tool(self):
        async def broken_handler(**kwargs: Any) -> None:
            raise RuntimeError("async handler kaboom")

        tool = _NativeAsyncTool()
        self.adapter.patch(self._sync_handler, broken_handler)
        result = await tool.ainvoke({"a": 3, "b": 4})
        assert result == 7

    @pytest.mark.asyncio
    async def test_ainvoke_dedup_guard_skips_when_tracking_active(self):
        from aevs.adapters.base import _aevs_tracking_active

        tool = _NativeAsyncTool()
        self.adapter.patch(self._sync_handler, self._async_handler)

        token = _aevs_tracking_active.set(True)
        try:
            result = await tool.ainvoke({"a": 1, "b": 2})
        finally:
            _aevs_tracking_active.reset(token)

        assert result == 3
        assert len(self.sync_calls) == 0
        assert len(self.async_calls) == 0


class TestRunIdCaptureProperties:
    """Verify _RunIdCapture injects correctly and captures tool_run_id."""

    def setup_method(self):
        self.calls: list[dict] = []
        self.captured_captures: list[Any] = []
        self.adapter = LangChainAdapter()

    def _sync_handler(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    async def _async_handler(self, **kwargs: Any) -> None:
        pass

    def teardown_method(self):
        self.adapter.unpatch()

    def test_capture_injects_and_captures_run_id(self):
        """_RunIdCapture is injected into config and tool_run_id is set."""
        from langchain_core.tools import tool

        @tool
        def dummy(x: int) -> int:
            """Dummy."""
            return x

        original_inject = _inject_capture

        def intercepting_inject(config, capture):
            self.captured_captures.append(capture)
            original_inject(config, capture)

        self.adapter.patch(self._sync_handler, self._async_handler)

        with patch("aevs.adapters.langchain._inject_capture",
                    side_effect=intercepting_inject):
            dummy.invoke({"x": 42})

        assert len(self.captured_captures) == 1
        capture = self.captured_captures[0]
        # After tool invocation, run_id is populated by LangChain's callback system
        assert capture.tool_run_id is not None


class TestLangChainAdapterMiscEdgeCases:
    def test_is_available_when_langchain_missing(self):
        with patch.dict("sys.modules", {"langchain_core.tools": None}):
            adapter = LangChainAdapter()
            assert adapter.is_available() is False
