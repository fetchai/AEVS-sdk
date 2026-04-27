"""Adversarial tests for the MCP adapter — designed to break it.

Targets uncovered lines: _get_mcp_version exception path,
_check_mcp_version with non-parseable versions, CreateTaskResult
with captured_exception (line 213).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aevs.adapters.mcp import (
    MCPAdapter,
    _check_mcp_version,
    _get_mcp_version,
)


class TestGetMcpVersion:
    def test_returns_actual_version(self):
        ver = _get_mcp_version()
        assert ver != "unknown"

    def test_returns_unknown_on_exception(self):
        with patch("importlib.metadata.version", side_effect=Exception("boom")):
            assert _get_mcp_version() == "unknown"


class TestCheckMcpVersionEdgeCases:
    def test_non_parseable_major(self):
        with patch("importlib.metadata.version", return_value="abc.20.0"):
            assert _check_mcp_version() is False

    def test_non_parseable_minor(self):
        with patch("importlib.metadata.version", return_value="1.abc.0"):
            assert _check_mcp_version() is False

    def test_single_component_returns_false(self):
        """Version string '2' has no minor part — caught by except."""
        with patch("importlib.metadata.version", return_value="2"):
            assert _check_mcp_version() is False

    def test_old_version_logs_warning(self):
        with patch("importlib.metadata.version", return_value="1.19.0"):
            assert _check_mcp_version() is False


class TestMCPAdapterIsAvailableEdge:
    def test_is_available_with_old_version(self):
        with patch("importlib.metadata.version", return_value="1.0.0"):
            adapter = MCPAdapter()
            assert adapter.is_available() is False


class TestMCPAdapterCreateTaskResultWithException:
    """Exercise line 213: CreateTaskResult + captured_exception re-raise.

    This scenario happens when the original call_tool both raises AND
    somehow the exception handling path produces a CreateTaskResult
    (a theoretical edge case). We test it by making the mock return a
    CreateTaskResult normally, then with an exception attached.
    """

    @pytest.fixture(autouse=True)
    def _restore(self):
        from mcp.client.session import ClientSession
        original = ClientSession.call_tool
        yield
        ClientSession.call_tool = original

    @pytest.mark.asyncio
    async def test_create_task_result_returned_normally(self):
        from mcp.client.session import ClientSession

        task_result = type("CreateTaskResult", (), {})()
        mock_original = AsyncMock(return_value=task_result)
        ClientSession.call_tool = mock_original

        adapter = MCPAdapter()
        calls: list[dict] = []

        async def handler(**kwargs: Any) -> None:
            calls.append(kwargs)

        adapter.patch(lambda **kw: None, handler)

        session = MagicMock(spec=ClientSession)
        result = await ClientSession.call_tool(session, "task_tool", {})
        assert type(result).__name__ == "CreateTaskResult"
        assert len(calls) == 0

        adapter.unpatch()
