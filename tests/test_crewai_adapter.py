from __future__ import annotations

import concurrent.futures
import contextvars
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest

from aevs.adapters.base import _aevs_tracking_active
from aevs.adapters.crewai import (
    CrewAIAdapter,
    _check_crewai_version,
    _extract_invoke_inputs,
    _extract_run_inputs,
    _invocation_id,
)

# ---------------------------------------------------------------------------
# Fixture: save/restore patched methods so tests don't leak state
# ---------------------------------------------------------------------------

@pytest.fixture()
def _restore_crewai_tools():
    from crewai.tools.base_tool import BaseTool, Tool
    from crewai.tools.structured_tool import CrewStructuredTool

    orig_bt_run = BaseTool.run
    orig_t_run = Tool.run
    orig_cst_invoke = CrewStructuredTool.invoke
    orig_cst_ainvoke = CrewStructuredTool.ainvoke
    yield
    BaseTool.run = orig_bt_run  # type: ignore[assignment]
    Tool.run = orig_t_run  # type: ignore[assignment]
    CrewStructuredTool.invoke = orig_cst_invoke  # type: ignore[assignment]
    CrewStructuredTool.ainvoke = orig_cst_ainvoke  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Tool factories
# ---------------------------------------------------------------------------

def _make_basetool():
    from crewai.tools.base_tool import BaseTool

    class AddTool(BaseTool):
        name: str = "add_tool"
        description: str = "Add two numbers."

        def _run(self, a: int = 0, b: int = 0) -> int:
            return a + b

    return AddTool()


def _make_failing_basetool():
    from crewai.tools.base_tool import BaseTool

    class FailTool(BaseTool):
        name: str = "fail_tool"
        description: str = "Always fails."

        def _run(self, msg: str = "boom") -> str:
            raise ValueError(msg)

    return FailTool()


def _make_none_returning_basetool():
    from crewai.tools.base_tool import BaseTool

    class VoidTool(BaseTool):
        name: str = "void_tool"
        description: str = "Returns nothing."

        def _run(self) -> None:
            pass

    return VoidTool()


def _make_decorator_tool():
    from crewai.tools import tool

    @tool("multiply")
    def multiply(a: int, b: int) -> int:
        """Multiply two numbers."""
        return a * b

    return multiply


def _make_structured_tool():
    from crewai.tools.structured_tool import CrewStructuredTool

    def greet(name: str) -> str:
        """Greet someone."""
        return f"Hello, {name}!"

    return CrewStructuredTool.from_function(greet)


def _make_async_structured_tool():
    from crewai.tools.structured_tool import CrewStructuredTool

    async def async_greet(name: str) -> str:
        """Greet someone asynchronously."""
        return f"Hi, {name}!"

    return CrewStructuredTool.from_function(async_greet)


def _make_nesting_tools():
    """Create an outer tool whose _run calls inner tool.run — tests dedup."""
    from crewai.tools.base_tool import BaseTool

    class InnerTool(BaseTool):
        name: str = "inner"
        description: str = "Inner tool."

        def _run(self) -> str:
            return "inner_result"

    inner = InnerTool()

    class OuterTool(BaseTool):
        name: str = "outer"
        description: str = "Calls inner tool."

        def _run(self) -> str:
            return f"outer+{inner.run()}"

    return OuterTool(), inner


# ---------------------------------------------------------------------------
# _check_crewai_version
# ---------------------------------------------------------------------------

class TestCheckCrewaiVersion:
    def test_valid_version(self):
        with patch("importlib.metadata.version", return_value="1.14.6"):
            assert _check_crewai_version() is True

    def test_minimum_version(self):
        with patch("importlib.metadata.version", return_value="1.0.0"):
            assert _check_crewai_version() is True

    def test_old_version(self):
        with patch("importlib.metadata.version", return_value="0.99.0"):
            assert _check_crewai_version() is False

    def test_major_2(self):
        with patch("importlib.metadata.version", return_value="2.0.0"):
            assert _check_crewai_version() is True

    def test_pre_release_version(self):
        with patch("importlib.metadata.version", return_value="1.14.6a1"):
            assert _check_crewai_version() is True

    def test_import_error(self):
        with patch("importlib.metadata.version", side_effect=Exception("not installed")):
            assert _check_crewai_version() is False

    def test_single_component_version(self):
        """Version like '2' with no minor component."""
        with patch("importlib.metadata.version", return_value="2"):
            assert _check_crewai_version() is False


# ---------------------------------------------------------------------------
# Input extraction — unit tests for edge cases
# ---------------------------------------------------------------------------

class TestExtractRunInputs:
    def test_kwargs_preferred(self):
        assert _extract_run_inputs((), {"a": 1, "b": 2}) == {"a": 1, "b": 2}

    def test_positional_fallback(self):
        result = _extract_run_inputs((10, 20), {})
        assert result == {"args": [10, 20]}

    def test_no_args_returns_empty(self):
        assert _extract_run_inputs((), {}) == {}

    def test_kwargs_takes_priority_over_args(self):
        """When both positional and keyword args exist, kwargs wins."""
        assert _extract_run_inputs((99,), {"x": 1}) == {"x": 1}


class TestExtractInvokeInputs:
    def test_dict_input(self):
        assert _extract_invoke_inputs(({"a": 1},), {}) == {"a": 1}

    def test_string_input(self):
        """CrewStructuredTool.invoke accepts str — must not crash."""
        assert _extract_invoke_inputs(('{"a": 1}',), {}) == '{"a": 1}'

    def test_kwarg_input_fallback(self):
        assert _extract_invoke_inputs((), {"input": {"x": 5}}) == {"x": 5}

    def test_no_input_at_all(self):
        assert _extract_invoke_inputs((), {}) == {}


# ---------------------------------------------------------------------------
# CrewAIAdapter basics
# ---------------------------------------------------------------------------

class TestCrewAIAdapterBasics:
    def setup_method(self):
        self.adapter = CrewAIAdapter()

    def test_name(self):
        assert self.adapter.name == "crewai"

    def test_is_available(self):
        assert self.adapter.is_available() is True

    def test_is_available_without_crewai(self):
        with patch.dict("sys.modules", {"crewai.tools.base_tool": None}):
            adapter = CrewAIAdapter()
            assert adapter.is_available() is False


# ---------------------------------------------------------------------------
# Native path: BaseTool.run
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_restore_crewai_tools")
class TestBaseToolRun:
    def setup_method(self):
        self.calls: list[dict] = []
        self.adapter = CrewAIAdapter()

    def _sync_handler(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    async def _async_handler(self, **kwargs: Any) -> None:
        pass

    def teardown_method(self):
        self.adapter.unpatch()

    def test_intercepts_and_records_all_fields(self):
        tool = _make_basetool()
        self.adapter.patch(self._sync_handler, self._async_handler)
        result = tool.run(a=3, b=4)
        assert result == 7

        assert len(self.calls) == 1
        call = self.calls[0]
        assert call["tool_name"] == "add_tool"
        assert call["inputs"] == {"a": 3, "b": 4}
        assert call["output"] == 7
        assert call["status"] == "success"
        assert call["error"] is None
        assert call["framework"] == "crewai"
        assert isinstance(call["framework_version"], str)
        assert call["framework_version"] != "unknown"
        assert isinstance(call["started_at"], datetime)
        assert isinstance(call["ended_at"], datetime)
        assert call["started_at"] <= call["ended_at"]
        assert call["tool_call_id"] is not None
        assert call["run_id"] is None
        assert call["parent_run_id"] is None
        assert call["invocation_id"] is None

    def test_tool_returning_none(self):
        """Tools that return None must be recorded without crashing."""
        tool = _make_none_returning_basetool()
        self.adapter.patch(self._sync_handler, self._async_handler)
        result = tool.run()
        assert result is None

        assert len(self.calls) == 1
        assert self.calls[0]["output"] is None
        assert self.calls[0]["status"] == "success"

    def test_preserves_exception(self):
        tool = _make_failing_basetool()
        self.adapter.patch(self._sync_handler, self._async_handler)
        with pytest.raises(ValueError, match="boom"):
            tool.run(msg="boom")

        assert len(self.calls) == 1
        assert self.calls[0]["status"] == "error"
        assert self.calls[0]["error"] == "boom"
        assert self.calls[0]["output"] is None

    def test_handler_failure_does_not_break_tool(self):
        def broken_handler(**kwargs: Any) -> None:
            raise RuntimeError("handler crashed")

        tool = _make_basetool()
        self.adapter.patch(broken_handler, self._async_handler)
        result = tool.run(a=1, b=2)
        assert result == 3


# ---------------------------------------------------------------------------
# Native path: Tool.run (@tool decorator)
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_restore_crewai_tools")
class TestDecoratorToolRun:
    def setup_method(self):
        self.calls: list[dict] = []
        self.adapter = CrewAIAdapter()

    def _sync_handler(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    async def _async_handler(self, **kwargs: Any) -> None:
        pass

    def teardown_method(self):
        self.adapter.unpatch()

    def test_intercepts_decorator_tool(self):
        tool = _make_decorator_tool()
        self.adapter.patch(self._sync_handler, self._async_handler)
        result = tool.run(a=5, b=6)
        assert result == 30

        assert len(self.calls) == 1
        assert self.calls[0]["tool_name"] == "multiply"
        assert self.calls[0]["output"] == 30

    def test_distinct_tool_call_ids(self):
        tool = _make_decorator_tool()
        self.adapter.patch(self._sync_handler, self._async_handler)
        tool.run(a=1, b=2)
        tool.run(a=3, b=4)

        assert len(self.calls) == 2
        ids = {c["tool_call_id"] for c in self.calls}
        assert len(ids) == 2


# ---------------------------------------------------------------------------
# Text path: CrewStructuredTool.invoke / ainvoke
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_restore_crewai_tools")
class TestStructuredToolInvoke:
    def setup_method(self):
        self.calls: list[dict] = []
        self.async_calls: list[dict] = []
        self.adapter = CrewAIAdapter()

    def _sync_handler(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    async def _async_handler(self, **kwargs: Any) -> None:
        self.async_calls.append(kwargs)

    def teardown_method(self):
        self.adapter.unpatch()

    def test_intercepts_invoke(self):
        tool = _make_structured_tool()
        self.adapter.patch(self._sync_handler, self._async_handler)
        result = tool.invoke({"name": "World"})
        assert result == "Hello, World!"

        assert len(self.calls) == 1
        call = self.calls[0]
        assert call["tool_name"] == "greet"
        assert call["inputs"] == {"name": "World"}
        assert call["output"] == "Hello, World!"
        assert call["framework"] == "crewai"

    def test_invoke_with_string_input(self):
        """invoke accepts str — input extraction must handle this."""
        tool = _make_structured_tool()
        self.adapter.patch(self._sync_handler, self._async_handler)
        result = tool.invoke('{"name": "StringInput"}')
        assert result == "Hello, StringInput!"

        assert len(self.calls) == 1
        assert self.calls[0]["inputs"] == '{"name": "StringInput"}'

    @pytest.mark.asyncio
    async def test_intercepts_ainvoke(self):
        tool = _make_async_structured_tool()
        self.adapter.patch(self._sync_handler, self._async_handler)
        result = await tool.ainvoke({"name": "Async"})
        assert result == "Hi, Async!"

        assert len(self.async_calls) == 1
        assert self.async_calls[0]["output"] == "Hi, Async!"

    def test_invoke_preserves_exception(self):
        from crewai.tools.structured_tool import CrewStructuredTool

        def explode(x: int) -> str:
            """Always explodes."""
            raise RuntimeError("kaboom")

        tool = CrewStructuredTool.from_function(explode)
        self.adapter.patch(self._sync_handler, self._async_handler)

        with pytest.raises(RuntimeError, match="kaboom"):
            tool.invoke({"x": 1})

        assert len(self.calls) == 1
        assert self.calls[0]["status"] == "error"
        assert self.calls[0]["error"] == "kaboom"

    @pytest.mark.asyncio
    async def test_ainvoke_async_handler_failure_does_not_break_tool(self):
        """Mirrors the sync handler-failure test — must also work on the async path."""
        async def broken_async_handler(**kwargs: Any) -> None:
            raise RuntimeError("async handler crashed")

        tool = _make_async_structured_tool()
        self.adapter.patch(self._sync_handler, broken_async_handler)
        result = await tool.ainvoke({"name": "Safe"})
        assert result == "Hi, Safe!"


# ---------------------------------------------------------------------------
# Cross-adapter deduplication
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_restore_crewai_tools")
class TestCrossAdapterDedup:
    def setup_method(self):
        self.calls: list[dict] = []
        self.adapter = CrewAIAdapter()

    def _sync_handler(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    async def _async_handler(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    def teardown_method(self):
        self.adapter.unpatch()

    def test_skips_when_tracking_active(self):
        tool = _make_basetool()
        self.adapter.patch(self._sync_handler, self._async_handler)

        token = _aevs_tracking_active.set(True)
        try:
            result = tool.run(a=1, b=2)
        finally:
            _aevs_tracking_active.reset(token)

        assert result == 3
        assert len(self.calls) == 0

    def test_tracking_cleared_after_call(self):
        tool = _make_basetool()
        self.adapter.patch(self._sync_handler, self._async_handler)

        tool.run(a=1, b=2)
        tool.run(a=3, b=4)

        assert len(self.calls) == 2
        assert _aevs_tracking_active.get(False) is False

    def test_tracking_cleared_on_exception(self):
        tool = _make_failing_basetool()
        self.adapter.patch(self._sync_handler, self._async_handler)

        with pytest.raises(ValueError):
            tool.run(msg="err")

        assert _aevs_tracking_active.get(False) is False

    def test_nested_tool_calls_produce_single_receipt(self):
        """When outer tool._run calls inner tool.run(), only the outer
        should produce a receipt — the inner must be skipped by the
        _aevs_tracking_active guard."""
        outer, _inner = _make_nesting_tools()
        self.adapter.patch(self._sync_handler, self._async_handler)

        result = outer.run()
        assert "inner_result" in result

        assert len(self.calls) == 1, (
            "nested call must be deduped; expected 1 receipt, got "
            f"{len(self.calls)}"
        )
        assert self.calls[0]["tool_name"] == "outer"

    @pytest.mark.asyncio
    async def test_ainvoke_skips_when_tracking_active(self):
        tool = _make_async_structured_tool()
        self.adapter.patch(self._sync_handler, self._async_handler)

        token = _aevs_tracking_active.set(True)
        try:
            result = await tool.ainvoke({"name": "Test"})
        finally:
            _aevs_tracking_active.reset(token)

        assert result == "Hi, Test!"
        assert len(self.calls) == 0

    def test_concurrent_sync_calls_independent(self):
        """CrewAI dispatches native tool calls in a ThreadPoolExecutor with
        copy_context(). Each thread must get its own tracking token and
        produce an independent receipt."""
        tool = _make_basetool()
        self.adapter.patch(self._sync_handler, self._async_handler)

        def call_in_context(a: int, b: int) -> int:
            ctx = contextvars.copy_context()
            return ctx.run(tool.run, a=a, b=b)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(call_in_context, 1, 2)
            f2 = pool.submit(call_in_context, 3, 4)
            assert f1.result() == 3
            assert f2.result() == 7

        assert len(self.calls) == 2
        ids = {c["tool_call_id"] for c in self.calls}
        assert len(ids) == 2, "concurrent calls must have distinct tool_call_ids"


# ---------------------------------------------------------------------------
# Patch / unpatch idempotency
# ---------------------------------------------------------------------------

@pytest.mark.usefixtures("_restore_crewai_tools")
class TestPatchIdempotency:
    def setup_method(self):
        self.calls: list[dict] = []
        self.adapter = CrewAIAdapter()

    def _sync_handler(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    async def _async_handler(self, **kwargs: Any) -> None:
        pass

    def teardown_method(self):
        self.adapter.unpatch()

    def test_idempotent_patch(self):
        self.adapter.patch(self._sync_handler, self._async_handler)
        first_orig = self.adapter._orig_basetool_run
        self.adapter.patch(self._sync_handler, self._async_handler)
        assert self.adapter._orig_basetool_run is first_orig

    def test_unpatch_restores(self):
        from crewai.tools.base_tool import BaseTool

        original_fn = BaseTool.run
        self.adapter.patch(self._sync_handler, self._async_handler)
        assert BaseTool.run is not original_fn

        self.adapter.unpatch()
        assert BaseTool.run is original_fn

    def test_idempotent_unpatch(self):
        self.adapter.unpatch()
        self.adapter.patch(self._sync_handler, self._async_handler)
        self.adapter.unpatch()
        self.adapter.unpatch()

    def test_arun_left_untouched(self):
        """BaseTool.arun must NOT be patched — never called by CrewAI executors.

        Older CrewAI versions expose ``BaseTool.arun``; newer ones (>=1.x)
        removed it. Either way the adapter must never introduce or replace
        it, so we compare against whatever the attribute was (possibly absent).
        """
        from crewai.tools.base_tool import BaseTool

        original_arun = getattr(BaseTool, "arun", None)
        self.adapter.patch(self._sync_handler, self._async_handler)
        assert getattr(BaseTool, "arun", None) is original_arun
        self.adapter.unpatch()
        assert getattr(BaseTool, "arun", None) is original_arun

    def test_unpatch_then_tool_call_unintercepted(self):
        """After unpatch, tool calls must not be intercepted."""
        tool = _make_basetool()
        self.adapter.patch(self._sync_handler, self._async_handler)
        self.adapter.unpatch()

        result = tool.run(a=10, b=20)
        assert result == 30
        assert len(self.calls) == 0


# ---------------------------------------------------------------------------
# Invocation ID grouping via Crew.kickoff / akickoff
#
# Strategy: We can't easily construct a full Crew object (needs agents, tasks,
# LLM config). Instead we test the _patch_crew mechanism directly by monkey-
# patching Crew.kickoff with a fake before the adapter patches, then verifying
# that the adapter's wrapper sets/resets the ContextVar correctly.
# ---------------------------------------------------------------------------

@pytest.fixture()
def _restore_crew_kickoff():
    """Save/restore Crew.kickoff around each test."""
    from crewai import Crew

    orig_kickoff = Crew.kickoff
    orig_akickoff = getattr(Crew, "akickoff", None)
    yield
    Crew.kickoff = orig_kickoff  # type: ignore[assignment]
    if orig_akickoff is not None:
        Crew.akickoff = orig_akickoff  # type: ignore[assignment]


@pytest.mark.usefixtures("_restore_crewai_tools", "_restore_crew_kickoff")
class TestInvocationIdGrouping:
    """Test that Crew.kickoff/akickoff set _invocation_id and that tool
    calls outside a crew run get null invocation_id."""

    def setup_method(self):
        self.calls: list[dict] = []
        self.adapter = CrewAIAdapter()

    def _sync_handler(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    async def _async_handler(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)

    def teardown_method(self):
        self.adapter.unpatch()

    def test_invocation_id_null_outside_crew(self):
        """Direct tool.run() outside Crew.kickoff must have null invocation_id."""
        tool = _make_basetool()
        self.adapter.patch(self._sync_handler, self._async_handler)

        tool.run(a=1, b=1)

        assert len(self.calls) == 1
        assert self.calls[0]["invocation_id"] is None

    def test_kickoff_sets_invocation_id(self):
        """Tool calls inside a patched kickoff must share a non-null invocation_id."""
        from crewai import Crew

        tool = _make_basetool()

        captured_inv_ids: list[str | None] = []

        def fake_kickoff(crew_self: Any, *args: Any, **kwargs: Any) -> str:
            captured_inv_ids.append(_invocation_id.get(None))
            tool.run(a=1, b=2)
            tool.run(a=3, b=4)
            return "done"

        # Install fake before the adapter patches so it becomes the "original"
        Crew.kickoff = fake_kickoff  # type: ignore[assignment]
        self.adapter.patch(self._sync_handler, self._async_handler)

        crew = object.__new__(Crew)
        crew.__dict__["__pydantic_private__"] = {}
        crew.__dict__["__pydantic_extra__"] = {}
        Crew.kickoff(crew)  # type: ignore[arg-type]

        assert len(captured_inv_ids) == 1
        assert captured_inv_ids[0] is not None, (
            "invocation_id must be set inside kickoff"
        )

        assert len(self.calls) == 2
        inv_ids = {c["invocation_id"] for c in self.calls}
        assert len(inv_ids) == 1, "all tool calls in one kickoff must share one invocation_id"
        assert None not in inv_ids

    def test_invocation_id_reset_after_kickoff(self):
        """After kickoff returns, _invocation_id must be reset to None."""
        from crewai import Crew

        assert _invocation_id.get(None) is None

        def noop_kickoff(crew_self: Any, *args: Any, **kwargs: Any) -> str:
            return "ok"

        Crew.kickoff = noop_kickoff  # type: ignore[assignment]
        self.adapter.patch(self._sync_handler, self._async_handler)

        crew = object.__new__(Crew)
        crew.__dict__["__pydantic_private__"] = {}
        crew.__dict__["__pydantic_extra__"] = {}
        Crew.kickoff(crew)  # type: ignore[arg-type]

        assert _invocation_id.get(None) is None, (
            "invocation_id must be reset after kickoff returns"
        )

    def test_invocation_id_reset_on_kickoff_exception(self):
        """If kickoff raises, _invocation_id must still be reset."""
        from crewai import Crew

        def exploding_kickoff(crew_self: Any, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("crew failed")

        Crew.kickoff = exploding_kickoff  # type: ignore[assignment]
        self.adapter.patch(self._sync_handler, self._async_handler)

        crew = object.__new__(Crew)
        crew.__dict__["__pydantic_private__"] = {}
        crew.__dict__["__pydantic_extra__"] = {}
        with pytest.raises(RuntimeError, match="crew failed"):
            Crew.kickoff(crew)  # type: ignore[arg-type]

        assert _invocation_id.get(None) is None

    def test_separate_kickoffs_get_distinct_invocation_ids(self):
        """Two sequential kickoff calls must get different invocation_ids."""
        from crewai import Crew

        tool = _make_basetool()
        inv_ids_per_run: list[str | None] = []

        def recording_kickoff(crew_self: Any, *args: Any, **kwargs: Any) -> str:
            inv_ids_per_run.append(_invocation_id.get(None))
            tool.run(a=1, b=1)
            return "done"

        Crew.kickoff = recording_kickoff  # type: ignore[assignment]
        self.adapter.patch(self._sync_handler, self._async_handler)

        crew = object.__new__(Crew)
        crew.__dict__["__pydantic_private__"] = {}
        crew.__dict__["__pydantic_extra__"] = {}
        Crew.kickoff(crew)  # type: ignore[arg-type]
        Crew.kickoff(crew)  # type: ignore[arg-type]

        assert len(inv_ids_per_run) == 2
        assert inv_ids_per_run[0] != inv_ids_per_run[1], (
            "separate kickoff calls must produce distinct invocation_ids"
        )
        assert len(self.calls) == 2
        assert self.calls[0]["invocation_id"] == inv_ids_per_run[0]
        assert self.calls[1]["invocation_id"] == inv_ids_per_run[1]
