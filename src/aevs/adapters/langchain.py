from __future__ import annotations

import functools
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from aevs.adapters.base import (
    AsyncToolCallHandler,
    BaseAdapter,
    ToolCallHandler,
    _aevs_tracking_active,
)

logger = logging.getLogger("aevs")


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


class LangChainAdapter(BaseAdapter):
    """Patches langchain_core.tools.BaseTool.invoke / ainvoke."""

    def __init__(self) -> None:
        self._original_invoke: Any = None
        self._original_ainvoke: Any = None
        self._patched = False

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
                    inputs=input,
                    output=output,
                    status=status,
                    error=error,
                    started_at=started_at,
                    ended_at=ended_at,
                    run_id=capture.tool_run_id,
                    parent_run_id=parent_run_id,
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
                    inputs=input,
                    output=output,
                    status=status,
                    error=error,
                    started_at=started_at,
                    ended_at=ended_at,
                    run_id=capture.tool_run_id,
                    parent_run_id=parent_run_id,
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
        logger.info("AEVS: LangChain adapter patched")

    def unpatch(self) -> None:
        if not self._patched:
            return

        from langchain_core.tools import BaseTool

        BaseTool.invoke = self._original_invoke  # type: ignore[method-assign]
        BaseTool.ainvoke = self._original_ainvoke  # type: ignore[method-assign]
        self._original_invoke = None
        self._original_ainvoke = None
        self._patched = False
        logger.info("AEVS: LangChain adapter unpatched")
