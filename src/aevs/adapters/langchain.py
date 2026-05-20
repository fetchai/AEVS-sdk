from __future__ import annotations

import functools
import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

from aevs.adapters.base import (
    AsyncToolCallHandler,
    BaseAdapter,
    ToolCallHandler,
    _aevs_tracking_active,
)

logger = logging.getLogger("aevs")

# ContextVar holding the current invocation ID. Set at the CompiledStateGraph
# entry point (invoke/ainvoke/stream/astream) so all tool calls within a single
# graph execution share the same value regardless of how many steps they span.
_invocation_id: ContextVar[str | None] = ContextVar("aevs_invocation_id", default=None)


def _get_invocation_id() -> str | None:
    """Return the current invocation ID if inside a patched graph execution.

    Falls back to LangSmith's trace_id when available (best-effort, no hard
    dependency on langsmith).
    """
    inv = _invocation_id.get(None)
    if inv:
        return inv
    try:
        from langsmith.run_helpers import get_current_run_tree

        rt = get_current_run_tree()
        if rt and rt.trace_id:
            return str(rt.trace_id)
    except Exception:
        pass
    return None


def _get_langchain_version() -> str:
    try:
        from importlib.metadata import version

        return version("langchain-core")
    except Exception:
        return "unknown"


def _extract_parent_run_id(config: Any) -> str | None:
    if isinstance(config, dict):
        cbs = config.get("callbacks")
        if cbs is not None and hasattr(cbs, "parent_run_id"):
            pid = cbs.parent_run_id
            return str(pid) if pid is not None else None
    return None


def _inject_capture(config: dict[str, Any], capture: Any) -> None:
    """Add *capture* to the callback list in *config* (mutates in place)."""
    cbs = config.get("callbacks")
    if cbs is None:
        config["callbacks"] = [capture]
    elif isinstance(cbs, list):
        cbs.append(capture)
    elif hasattr(cbs, "add_handler"):
        cbs.add_handler(capture)


def _normalize_inputs(input: Any) -> Any:
    """Strip the LangChain tool_call envelope so receipts record only the
    tool's argument dict — matching the MCP adapter's convention.

    LangChain's ``BaseTool.invoke`` accepts either:
      * a raw arguments dict / string (passed straight through), or
      * a ``ToolCall`` envelope ``{"id": str, "name": str, "args": dict,
        "type": "tool_call"}`` (what ``langchain.agents.create_agent`` and
        every LangGraph ReAct loop pass).

    We unwrap the envelope so the AEVS receipt's ``inputs`` field is always
    just the args — ``id``/``name``/``type`` are already captured separately
    as ``tool_call_id`` / ``tool_name`` / ``framework``.
    """
    if isinstance(input, dict) and input.get("type") == "tool_call" and "args" in input:
        return input["args"]
    return input


class LangChainAdapter(BaseAdapter):
    """Patches langchain_core.tools.BaseTool.invoke / ainvoke."""

    def __init__(self) -> None:
        self._original_invoke: Any = None
        self._original_ainvoke: Any = None
        self._patched = False
        # Graph-level patching state
        self._graph_cls: Any = None
        self._original_graph_invoke: Any = None
        self._original_graph_ainvoke: Any = None
        self._original_graph_stream: Any = None
        self._original_graph_astream: Any = None
        self._graph_patched = False

    @property
    def name(self) -> str:
        return "langchain"

    def is_available(self) -> bool:
        try:
            import langchain_core.tools  # noqa: F401

            return True
        except ImportError:
            return False

    def patch(
        self,
        on_tool_call: ToolCallHandler,
        on_tool_call_async: AsyncToolCallHandler,
    ) -> None:
        if self._patched:
            return

        from langchain_core.callbacks import BaseCallbackHandler
        from langchain_core.runnables.config import ensure_config
        from langchain_core.tools import BaseTool

        class _RunIdCapture(BaseCallbackHandler):
            """Captures the run_id that LangChain generates inside
            ``callback_manager.on_tool_start()``.

            ``ignore_agent`` is intentionally left ``False`` (the default)
            because LangChain uses it as the skip-condition for
            ``on_tool_start``."""

            def __init__(self) -> None:
                super().__init__()
                self.tool_run_id: str | None = None

            def on_tool_start(
                self,
                serialized: dict[str, Any],
                input_str: str,
                *,
                run_id: UUID,
                **kwargs: Any,
            ) -> None:
                self.tool_run_id = str(run_id)

        self._original_invoke = BaseTool.invoke
        self._original_ainvoke = BaseTool.ainvoke
        lc_version = _get_langchain_version()

        original_invoke = self._original_invoke
        original_ainvoke = self._original_ainvoke

        @functools.wraps(original_invoke)
        def patched_invoke(tool_self: Any, input: Any, config: Any = None, **kwargs: Any) -> Any:
            if _aevs_tracking_active.get(False):
                return original_invoke(tool_self, input, config, **kwargs)

            token = _aevs_tracking_active.set(True)
            config = ensure_config(config)
            parent_run_id = _extract_parent_run_id(config)
            tool_call_id = input.get("id") if isinstance(input, dict) else None
            normalized_inputs = _normalize_inputs(input)
            capture = _RunIdCapture()
            _inject_capture(config, capture)

            started_at = datetime.now(timezone.utc)
            captured_exception: BaseException | None = None
            output: Any = None
            status = "success"
            error: str | None = None

            try:
                output = original_invoke(tool_self, input, config, **kwargs)
            except Exception as exc:
                status = "error"
                error = str(exc)
                captured_exception = exc
            finally:
                _aevs_tracking_active.reset(token)

            ended_at = datetime.now(timezone.utc)

            try:
                on_tool_call(
                    tool_name=tool_self.name,
                    inputs=normalized_inputs,
                    output=output,
                    status=status,
                    error=error,
                    started_at=started_at,
                    ended_at=ended_at,
                    run_id=capture.tool_run_id,
                    parent_run_id=parent_run_id,
                    invocation_id=_get_invocation_id(),
                    tool_call_id=tool_call_id,
                    framework="langchain",
                    framework_version=lc_version,
                )
            except Exception:
                logger.debug("AEVS: failed to process tool call", exc_info=True)

            if captured_exception is not None:
                raise captured_exception
            return output

        @functools.wraps(original_ainvoke)
        async def patched_ainvoke(
            tool_self: Any, input: Any, config: Any = None, **kwargs: Any
        ) -> Any:
            if _aevs_tracking_active.get(False):
                return await original_ainvoke(tool_self, input, config, **kwargs)

            token = _aevs_tracking_active.set(True)
            config = ensure_config(config)
            parent_run_id = _extract_parent_run_id(config)
            tool_call_id = input.get("id") if isinstance(input, dict) else None
            normalized_inputs = _normalize_inputs(input)
            capture = _RunIdCapture()
            _inject_capture(config, capture)

            started_at = datetime.now(timezone.utc)
            captured_exception: BaseException | None = None
            output: Any = None
            status = "success"
            error: str | None = None

            try:
                output = await original_ainvoke(tool_self, input, config, **kwargs)
            except Exception as exc:
                status = "error"
                error = str(exc)
                captured_exception = exc
            finally:
                _aevs_tracking_active.reset(token)

            ended_at = datetime.now(timezone.utc)

            try:
                await on_tool_call_async(
                    tool_name=tool_self.name,
                    inputs=normalized_inputs,
                    output=output,
                    status=status,
                    error=error,
                    started_at=started_at,
                    ended_at=ended_at,
                    run_id=capture.tool_run_id,
                    parent_run_id=parent_run_id,
                    invocation_id=_get_invocation_id(),
                    tool_call_id=tool_call_id,
                    framework="langchain",
                    framework_version=lc_version,
                )
            except Exception:
                logger.debug("AEVS: failed to process async tool call", exc_info=True)

            if captured_exception is not None:
                raise captured_exception
            return output

        BaseTool.invoke = patched_invoke  # type: ignore[assignment]
        BaseTool.ainvoke = patched_ainvoke  # type: ignore[assignment]
        self._patched = True
        self._patch_graph()
        logger.info("AEVS: LangChain adapter patched")

    def _patch_graph(self) -> None:
        """Patch CompiledStateGraph entry points to set invocation_id ContextVar.

        Conditionally imports langgraph — if not installed, this is a no-op.
        """
        if self._graph_patched:
            return
        try:
            from langgraph.graph.state import CompiledStateGraph
        except ImportError:
            logger.debug("AEVS: langgraph not installed, skipping graph-level patching")
            return

        self._graph_cls = CompiledStateGraph
        self._original_graph_invoke = CompiledStateGraph.invoke
        self._original_graph_ainvoke = CompiledStateGraph.ainvoke
        self._original_graph_stream = CompiledStateGraph.stream
        self._original_graph_astream = CompiledStateGraph.astream

        def _wrap_sync(original: Any) -> Any:
            @functools.wraps(original)
            def wrapper(graph_self: Any, *args: Any, **kwargs: Any) -> Any:
                if _invocation_id.get(None):
                    return original(graph_self, *args, **kwargs)
                token = _invocation_id.set(str(uuid4()))
                try:
                    return original(graph_self, *args, **kwargs)
                finally:
                    _invocation_id.reset(token)

            return wrapper

        def _wrap_async(original: Any) -> Any:
            @functools.wraps(original)
            async def wrapper(graph_self: Any, *args: Any, **kwargs: Any) -> Any:
                if _invocation_id.get(None):
                    return await original(graph_self, *args, **kwargs)
                token = _invocation_id.set(str(uuid4()))
                try:
                    return await original(graph_self, *args, **kwargs)
                finally:
                    _invocation_id.reset(token)

            return wrapper

        def _wrap_stream(original: Any) -> Any:
            @functools.wraps(original)
            def wrapper(graph_self: Any, *args: Any, **kwargs: Any) -> Any:
                if _invocation_id.get(None):
                    yield from original(graph_self, *args, **kwargs)
                    return
                token = _invocation_id.set(str(uuid4()))
                try:
                    yield from original(graph_self, *args, **kwargs)
                finally:
                    _invocation_id.reset(token)

            return wrapper

        def _wrap_astream(original: Any) -> Any:
            @functools.wraps(original)
            async def wrapper(graph_self: Any, *args: Any, **kwargs: Any) -> Any:
                if _invocation_id.get(None):
                    async for chunk in original(graph_self, *args, **kwargs):
                        yield chunk
                    return
                token = _invocation_id.set(str(uuid4()))
                try:
                    async for chunk in original(graph_self, *args, **kwargs):
                        yield chunk
                finally:
                    _invocation_id.reset(token)

            return wrapper

        CompiledStateGraph.invoke = _wrap_sync(self._original_graph_invoke)  # type: ignore[method-assign]
        CompiledStateGraph.ainvoke = _wrap_async(self._original_graph_ainvoke)  # type: ignore[method-assign]
        CompiledStateGraph.stream = _wrap_stream(self._original_graph_stream)  # type: ignore[method-assign]
        CompiledStateGraph.astream = _wrap_astream(self._original_graph_astream)  # type: ignore[method-assign]
        self._graph_patched = True
        logger.debug("AEVS: LangGraph CompiledStateGraph patched for invocation_id tracking")

    def _unpatch_graph(self) -> None:
        """Restore original CompiledStateGraph methods."""
        if not self._graph_patched:
            return
        cls = self._graph_cls
        cls.invoke = self._original_graph_invoke
        cls.ainvoke = self._original_graph_ainvoke
        cls.stream = self._original_graph_stream
        cls.astream = self._original_graph_astream
        self._graph_cls = None
        self._original_graph_invoke = None
        self._original_graph_ainvoke = None
        self._original_graph_stream = None
        self._original_graph_astream = None
        self._graph_patched = False
        logger.debug("AEVS: LangGraph CompiledStateGraph unpatched")

    def unpatch(self) -> None:
        if not self._patched:
            return

        from langchain_core.tools import BaseTool

        BaseTool.invoke = self._original_invoke  # type: ignore[method-assign]
        BaseTool.ainvoke = self._original_ainvoke  # type: ignore[method-assign]
        self._original_invoke = None
        self._original_ainvoke = None
        self._patched = False
        self._unpatch_graph()
        logger.info("AEVS: LangChain adapter unpatched")
