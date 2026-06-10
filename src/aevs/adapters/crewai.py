from __future__ import annotations

import functools
import logging
import re
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any

from aevs.adapters.base import (
    AsyncToolCallHandler,
    BaseAdapter,
    ToolCallHandler,
    _aevs_tracking_active,
)

logger = logging.getLogger("aevs")

_MIN_CREWAI_MAJOR = 1
_MIN_CREWAI_MINOR = 0

_LEADING_DIGITS = re.compile(r"\d+")

_invocation_id: ContextVar[str | None] = ContextVar("aevs_crewai_invocation_id", default=None)


def _get_crewai_version() -> str:
    try:
        from importlib.metadata import version

        return version("crewai")
    except Exception:
        return "unknown"


def _check_crewai_version() -> bool:
    """Return True if crewai>=1.0 is installed.

    Handles pre-release suffixes like ``1.14.6a1`` by extracting leading
    digits from each version component.
    """
    try:
        from importlib.metadata import version

        ver = version("crewai")
        parts = ver.split(".")
        m_major = _LEADING_DIGITS.match(parts[0])
        m_minor = _LEADING_DIGITS.match(parts[1]) if len(parts) > 1 else None
        if not m_major or not m_minor:
            logger.warning(
                "AEVS: unparsable crewai version %r — CrewAI adapter disabled",
                ver,
            )
            return False
        major, minor = int(m_major.group()), int(m_minor.group())
        if major > _MIN_CREWAI_MAJOR or (major == _MIN_CREWAI_MAJOR and minor >= _MIN_CREWAI_MINOR):
            return True
        logger.warning(
            "AEVS: crewai %s is below minimum %d.%d — CrewAI adapter disabled",
            ver, _MIN_CREWAI_MAJOR, _MIN_CREWAI_MINOR,
        )
        return False
    except Exception:
        logger.warning(
            "AEVS: could not parse crewai package version — CrewAI adapter disabled",
            exc_info=True,
        )
        return False


def _extract_run_inputs(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Extract inputs from BaseTool.run / Tool.run call signature.

    ``run(self, *args, **kwargs)`` — the executor always calls with
    keyword arguments, but handle positional args defensively.
    """
    if kwargs:
        return kwargs
    if args:
        return {"args": list(args)}
    return {}


def _extract_invoke_inputs(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    """Extract inputs from CrewStructuredTool.invoke / ainvoke.

    ``invoke(self, input, config=None, **kwargs)`` — first positional arg
    is the input dict/string.
    """
    if args:
        return args[0]
    return kwargs.get("input", {})


class CrewAIAdapter(BaseAdapter):
    """Patches CrewAI tool dispatch to intercept every tool call.

    Covers both execution paths:
    - Native function-calling: ``BaseTool.run`` / ``Tool.run`` (always sync)
    - Text ReAct fallback: ``CrewStructuredTool.invoke`` / ``ainvoke``

    Optionally patches ``Crew.kickoff`` / ``Crew.akickoff`` to set an
    invocation_id ContextVar so all tool calls within a single crew run
    share a common identifier.
    """

    def __init__(self) -> None:
        self._orig_basetool_run: Any = None
        self._orig_tool_run: Any = None
        self._orig_structured_invoke: Any = None
        self._orig_structured_ainvoke: Any = None
        self._patched = False

        self._crew_cls: Any = None
        self._orig_kickoff: Any = None
        self._orig_akickoff: Any = None
        self._crew_patched = False

    @property
    def name(self) -> str:
        return "crewai"

    def is_available(self) -> bool:
        try:
            import crewai.tools.base_tool  # noqa: F401

            return _check_crewai_version()
        except ImportError:
            return False

    def patch(
        self,
        on_tool_call: ToolCallHandler,
        on_tool_call_async: AsyncToolCallHandler,
    ) -> None:
        if self._patched:
            return

        from crewai.tools.base_tool import BaseTool, Tool
        from crewai.tools.structured_tool import CrewStructuredTool

        crewai_version = _get_crewai_version()

        self._orig_basetool_run = BaseTool.run
        self._orig_tool_run = Tool.run
        self._orig_structured_invoke = CrewStructuredTool.invoke
        self._orig_structured_ainvoke = CrewStructuredTool.ainvoke

        # --- Native path: BaseTool.run (sync) ---
        BaseTool.run = _make_sync_wrapper(  # type: ignore[method-assign]
            self._orig_basetool_run,
            _extract_run_inputs,
            on_tool_call,
            crewai_version,
        )
        # --- Native path: Tool.run (sync, separate override) ---
        Tool.run = _make_sync_wrapper(  # type: ignore[method-assign]
            self._orig_tool_run,
            _extract_run_inputs,
            on_tool_call,
            crewai_version,
        )
        # --- Text path: CrewStructuredTool.invoke (sync) ---
        CrewStructuredTool.invoke = _make_sync_wrapper(  # type: ignore[method-assign]
            self._orig_structured_invoke,
            _extract_invoke_inputs,
            on_tool_call,
            crewai_version,
        )
        # --- Text path: CrewStructuredTool.ainvoke (async) ---
        CrewStructuredTool.ainvoke = _make_async_wrapper(  # type: ignore[method-assign]
            self._orig_structured_ainvoke,
            _extract_invoke_inputs,
            on_tool_call_async,
            crewai_version,
        )

        self._patched = True
        self._patch_crew()
        logger.info("AEVS: CrewAI adapter patched")

    def _patch_crew(self) -> None:
        """Patch Crew entry points to set invocation_id ContextVar.

        Conditionally imports crewai.Crew — if not available, this is a no-op.
        """
        if self._crew_patched:
            return
        try:
            from crewai import Crew
        except ImportError:
            logger.debug("AEVS: crewai.Crew not importable, skipping crew-level patching")
            return

        self._crew_cls = Crew

        # Crew.kickoff (sync) — covers kickoff_async (delegates via to_thread)
        # and kickoff_for_each (calls kickoff per item).
        orig_kickoff = getattr(Crew, "kickoff", None)
        if orig_kickoff is not None:
            self._orig_kickoff = orig_kickoff

            @functools.wraps(orig_kickoff)
            def patched_kickoff(crew_self: Any, *args: Any, **kwargs: Any) -> Any:
                if _invocation_id.get(None):
                    return orig_kickoff(crew_self, *args, **kwargs)
                token = _invocation_id.set(str(uuid.uuid4()))
                try:
                    return orig_kickoff(crew_self, *args, **kwargs)
                finally:
                    _invocation_id.reset(token)

            Crew.kickoff = patched_kickoff  # type: ignore[assignment]

        # Crew.akickoff (native async) — covers akickoff_for_each.
        # Does NOT delegate to kickoff, so needs its own patch.
        orig_akickoff = getattr(Crew, "akickoff", None)
        if orig_akickoff is not None:
            self._orig_akickoff = orig_akickoff

            @functools.wraps(orig_akickoff)
            async def patched_akickoff(crew_self: Any, *args: Any, **kwargs: Any) -> Any:
                if _invocation_id.get(None):
                    return await orig_akickoff(crew_self, *args, **kwargs)
                token = _invocation_id.set(str(uuid.uuid4()))
                try:
                    return await orig_akickoff(crew_self, *args, **kwargs)
                finally:
                    _invocation_id.reset(token)

            Crew.akickoff = patched_akickoff  # type: ignore[assignment]

        self._crew_patched = True
        logger.debug("AEVS: CrewAI Crew patched for invocation_id tracking")

    def _unpatch_crew(self) -> None:
        if not self._crew_patched:
            return
        cls = self._crew_cls
        if self._orig_kickoff is not None:
            cls.kickoff = self._orig_kickoff
        if self._orig_akickoff is not None:
            cls.akickoff = self._orig_akickoff
        self._crew_cls = None
        self._orig_kickoff = None
        self._orig_akickoff = None
        self._crew_patched = False
        logger.debug("AEVS: CrewAI Crew unpatched")

    def unpatch(self) -> None:
        if not self._patched:
            return

        from crewai.tools.base_tool import BaseTool, Tool
        from crewai.tools.structured_tool import CrewStructuredTool

        BaseTool.run = self._orig_basetool_run  # type: ignore[method-assign]
        Tool.run = self._orig_tool_run  # type: ignore[method-assign]
        CrewStructuredTool.invoke = self._orig_structured_invoke  # type: ignore[method-assign]
        CrewStructuredTool.ainvoke = self._orig_structured_ainvoke  # type: ignore[method-assign]
        self._orig_basetool_run = None
        self._orig_tool_run = None
        self._orig_structured_invoke = None
        self._orig_structured_ainvoke = None
        self._patched = False
        self._unpatch_crew()
        logger.info("AEVS: CrewAI adapter unpatched")


# ---------------------------------------------------------------------------
# Wrapper factories
# ---------------------------------------------------------------------------


def _make_sync_wrapper(
    original: Any,
    extract_inputs: Any,
    on_tool_call: ToolCallHandler,
    crewai_version: str,
) -> Any:
    """Build a sync wrapper for BaseTool.run / Tool.run / CrewStructuredTool.invoke."""

    @functools.wraps(original)
    def wrapper(tool_self: Any, *args: Any, **kwargs: Any) -> Any:
        if _aevs_tracking_active.get(False):
            return original(tool_self, *args, **kwargs)

        token = _aevs_tracking_active.set(True)
        inputs = extract_inputs(args, kwargs)
        tool_call_id = str(uuid.uuid4())

        started_at = datetime.now(timezone.utc)
        captured_exception: BaseException | None = None
        output: Any = None
        status = "success"
        error: str | None = None

        try:
            output = original(tool_self, *args, **kwargs)
        except Exception as exc:
            status = "error"
            error = str(exc)
            captured_exception = exc
        finally:
            _aevs_tracking_active.reset(token)

        ended_at = datetime.now(timezone.utc)

        try:
            on_tool_call(
                tool_name=getattr(tool_self, "name", "unknown"),
                inputs=inputs,
                output=output,
                status=status,
                error=error,
                started_at=started_at,
                ended_at=ended_at,
                run_id=None,
                parent_run_id=None,
                invocation_id=_invocation_id.get(None),
                tool_call_id=tool_call_id,
                framework="crewai",
                framework_version=crewai_version,
            )
        except Exception:
            logger.debug("AEVS: failed to process CrewAI tool call", exc_info=True)

        if captured_exception is not None:
            raise captured_exception
        return output

    return wrapper


def _make_async_wrapper(
    original: Any,
    extract_inputs: Any,
    on_tool_call_async: AsyncToolCallHandler,
    crewai_version: str,
) -> Any:
    """Build an async wrapper for CrewStructuredTool.ainvoke."""

    @functools.wraps(original)
    async def wrapper(tool_self: Any, *args: Any, **kwargs: Any) -> Any:
        if _aevs_tracking_active.get(False):
            return await original(tool_self, *args, **kwargs)

        token = _aevs_tracking_active.set(True)
        inputs = extract_inputs(args, kwargs)
        tool_call_id = str(uuid.uuid4())

        started_at = datetime.now(timezone.utc)
        captured_exception: BaseException | None = None
        output: Any = None
        status = "success"
        error: str | None = None

        try:
            output = await original(tool_self, *args, **kwargs)
        except Exception as exc:
            status = "error"
            error = str(exc)
            captured_exception = exc
        finally:
            _aevs_tracking_active.reset(token)

        ended_at = datetime.now(timezone.utc)

        try:
            await on_tool_call_async(
                tool_name=getattr(tool_self, "name", "unknown"),
                inputs=inputs,
                output=output,
                status=status,
                error=error,
                started_at=started_at,
                ended_at=ended_at,
                run_id=None,
                parent_run_id=None,
                invocation_id=_invocation_id.get(None),
                tool_call_id=tool_call_id,
                framework="crewai",
                framework_version=crewai_version,
            )
        except Exception:
            logger.debug("AEVS: failed to process async CrewAI tool call", exc_info=True)

        if captured_exception is not None:
            raise captured_exception
        return output

    return wrapper
