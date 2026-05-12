from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from langchain_core.tools import tool

from aevs.adapters.langchain import LangChainAdapter


@tool
def add_numbers(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@tool
def failing_tool(msg: str) -> str:
    """Always fails."""
    raise ValueError(msg)


class TestLangChainAdapter:
    def setup_method(self):
        self.calls: list[dict] = []
        self.async_calls: list[dict] = []
        self.adapter = LangChainAdapter()

    def _sync_handler(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    async def _async_handler(self, **kwargs: Any) -> None:
        self.async_calls.append(kwargs)

    def teardown_method(self):
        self.adapter.unpatch()

    def test_is_available(self):
        assert self.adapter.is_available() is True

    def test_name(self):
        assert self.adapter.name == "langchain"

    def test_intercepts_invoke(self):
        self.adapter.patch(self._sync_handler, self._async_handler)
        result = add_numbers.invoke({"a": 2, "b": 3})
        assert result == 5

        assert len(self.calls) == 1
        call = self.calls[0]
        assert call["tool_name"] == "add_numbers"
        assert call["status"] == "success"
        assert call["output"] == 5
        assert call["error"] is None
        assert call["framework"] == "langchain"
        assert isinstance(call["started_at"], datetime)
        assert isinstance(call["ended_at"], datetime)

    def test_preserves_return_value(self):
        self.adapter.patch(self._sync_handler, self._async_handler)
        result = add_numbers.invoke({"a": 10, "b": 20})
        assert result == 30

    def test_preserves_exception(self):
        self.adapter.patch(self._sync_handler, self._async_handler)
        with pytest.raises(ValueError, match="boom"):
            failing_tool.invoke({"msg": "boom"})

        assert len(self.calls) == 1
        assert self.calls[0]["status"] == "error"
        assert self.calls[0]["error"] == "boom"

    def test_handler_failure_does_not_break_tool(self):
        def broken_handler(**kwargs: Any) -> None:
            raise RuntimeError("handler crashed")

        self.adapter.patch(broken_handler, self._async_handler)
        result = add_numbers.invoke({"a": 1, "b": 1})
        assert result == 2

    def test_idempotent_patch(self):
        self.adapter.patch(self._sync_handler, self._async_handler)
        self.adapter.patch(self._sync_handler, self._async_handler)
        add_numbers.invoke({"a": 1, "b": 1})
        assert len(self.calls) == 1  # only intercepted once

    def test_unpatch_restores(self):
        self.adapter.patch(self._sync_handler, self._async_handler)
        self.adapter.unpatch()
        add_numbers.invoke({"a": 1, "b": 1})
        assert len(self.calls) == 0  # not intercepted

    def test_idempotent_unpatch(self):
        self.adapter.unpatch()  # no-op when not patched
        self.adapter.patch(self._sync_handler, self._async_handler)
        self.adapter.unpatch()
        self.adapter.unpatch()  # no-op again

    @pytest.mark.asyncio
    async def test_ainvoke_intercepted_via_sync_path(self):
        """StructuredTool.ainvoke delegates to invoke via run_in_executor,
        so the sync handler intercepts the call (not the async handler).
        """
        self.adapter.patch(self._sync_handler, self._async_handler)
        result = await add_numbers.ainvoke({"a": 5, "b": 7})
        assert result == 12

        assert len(self.calls) == 1
        assert self.calls[0]["tool_name"] == "add_numbers"
        assert self.calls[0]["output"] == 12

    @pytest.mark.asyncio
    async def test_ainvoke_preserves_exception(self):
        self.adapter.patch(self._sync_handler, self._async_handler)
        with pytest.raises(ValueError, match="async_boom"):
            await failing_tool.ainvoke({"msg": "async_boom"})

        assert len(self.calls) == 1
        assert self.calls[0]["status"] == "error"

    @pytest.mark.asyncio
    async def test_async_handler_failure_does_not_break_tool(self):
        async def broken_async_handler(**kwargs: Any) -> None:
            raise RuntimeError("async handler crashed")

        self.adapter.patch(self._sync_handler, broken_async_handler)
        result = await add_numbers.ainvoke({"a": 3, "b": 4})
        assert result == 7

    def test_inputs_unwrapped_from_tool_call_envelope(self):
        """When the agent loop passes a ToolCall envelope (as
        langchain.agents.create_agent / LangGraph ReAct does), the captured
        ``inputs`` must be the raw args dict — not the full envelope. Matches
        the MCP adapter's convention so receipts have a uniform shape across
        frameworks. The envelope's id/name are still recorded separately as
        tool_call_id and tool_name.
        """
        self.adapter.patch(self._sync_handler, self._async_handler)
        tool_call = {
            "id": "call_3e070c95eeba4854899ec7c8",
            "args": {"a": 6, "b": 7},
            "name": "add_numbers",
            "type": "tool_call",
        }

        # LangChain returns a ToolMessage when invoked with a tool_call
        # envelope (so the agent loop can feed it into the next LLM turn).
        result = add_numbers.invoke(tool_call)
        assert getattr(result, "content", None) == "13"
        assert getattr(result, "tool_call_id", None) == "call_3e070c95eeba4854899ec7c8"

        assert len(self.calls) == 1
        call = self.calls[0]
        assert call["inputs"] == {"a": 6, "b": 7}, (
            "tool_call envelope must be unwrapped to its args dict"
        )
        assert call["tool_call_id"] == "call_3e070c95eeba4854899ec7c8"
        assert call["tool_name"] == "add_numbers"
        # Sanity: the tool actually executed (output carries the real value
        # somewhere — either raw or inside a ToolMessage wrapper).
        out = call["output"]
        assert out == 13 or getattr(out, "content", None) == "13"

    def test_inputs_passthrough_for_plain_args_dict(self):
        """A direct invoke with a plain args dict (not wrapped in a
        ToolCall envelope) must be recorded as-is.
        """
        self.adapter.patch(self._sync_handler, self._async_handler)
        result = add_numbers.invoke({"a": 2, "b": 3})
        assert result == 5

        assert len(self.calls) == 1
        assert self.calls[0]["inputs"] == {"a": 2, "b": 3}
        assert self.calls[0]["tool_call_id"] is None

    def test_inputs_passthrough_for_envelope_lookalike_without_type(self):
        """A dict that happens to have an ``args`` key but no
        ``type='tool_call'`` marker is NOT a ToolCall envelope and must be
        passed through unchanged. This guards against accidentally
        unwrapping a tool whose real argument *is* literally named ``args``.
        """
        from aevs.adapters.langchain import _normalize_inputs

        looks_like_envelope = {"args": {"x": 1}, "id": "abc"}
        assert _normalize_inputs(looks_like_envelope) is looks_like_envelope

    @pytest.mark.asyncio
    async def test_inputs_unwrapped_from_tool_call_envelope_async(self):
        """Async path mirrors the sync unwrap behaviour."""
        self.adapter.patch(self._sync_handler, self._async_handler)
        tool_call = {
            "id": "call_async_xyz",
            "args": {"a": 4, "b": 5},
            "name": "add_numbers",
            "type": "tool_call",
        }
        result = await add_numbers.ainvoke(tool_call)
        assert getattr(result, "content", None) == "9"

        # StructuredTool.ainvoke delegates to the sync path (see
        # test_ainvoke_intercepted_via_sync_path), so the call lands on
        # self.calls — not self.async_calls.
        assert len(self.calls) == 1
        assert self.calls[0]["inputs"] == {"a": 4, "b": 5}
        assert self.calls[0]["tool_call_id"] == "call_async_xyz"
        out = self.calls[0]["output"]
        assert out == 9 or getattr(out, "content", None) == "9"
