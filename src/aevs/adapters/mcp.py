from __future__ import annotations

import base64
import functools
import hashlib
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from aevs.adapters.base import (
    AsyncToolCallHandler,
    BaseAdapter,
    ToolCallHandler,
    _aevs_tracking_active,
)

logger = logging.getLogger("aevs")

_MIN_MCP_MAJOR = 1
_MIN_MCP_MINOR = 20

_LEADING_DIGITS = re.compile(r"\d+")


def _get_mcp_version() -> str:
    try:
        from importlib.metadata import version

        return version("mcp")
    except Exception:
        return "unknown"


def _check_mcp_version() -> bool:
    """Return True if mcp>=1.20 is installed.

    Handles pre-release suffixes like ``1.22a1`` by extracting leading
    digits from each version component.
    """
    try:
        from importlib.metadata import version

        ver = version("mcp")
        parts = ver.split(".")
        m_major = _LEADING_DIGITS.match(parts[0])
        m_minor = _LEADING_DIGITS.match(parts[1]) if len(parts) > 1 else None
        if not m_major or not m_minor:
            logger.warning(
                "AEVS: unparsable MCP version %r — MCP adapter disabled",
                ver,
            )
            return False
        major, minor = int(m_major.group()), int(m_minor.group())
        if major > _MIN_MCP_MAJOR or (major == _MIN_MCP_MAJOR and minor >= _MIN_MCP_MINOR):
            return True
        logger.warning(
            "AEVS: mcp %s is below minimum %d.%d — MCP adapter disabled",
            ver, _MIN_MCP_MAJOR, _MIN_MCP_MINOR,
        )
        return False
    except Exception:
        logger.warning(
            "AEVS: could not parse MCP package version — MCP adapter disabled",
            exc_info=True,
        )
        return False


def _serialize_content_block(block: Any) -> dict[str, Any]:
    """Convert a single MCP ContentBlock to a JSON-serialisable dict.

    Binary data (images, audio) is decoded from base64 first, then
    replaced with a SHA-256 hash and decoded byte count so receipts
    stay small and deterministic.
    """
    block_type = getattr(block, "type", None)

    if block_type == "text":
        return {"type": "text", "text": getattr(block, "text", "")}

    if block_type in ("image", "audio"):
        raw_data = getattr(block, "data", "") or ""
        try:
            decoded = base64.b64decode(raw_data)
        except Exception:
            decoded = raw_data.encode("utf-8") if isinstance(raw_data, str) else (raw_data or b"")
        return {
            "type": block_type,
            "mimeType": getattr(block, "mimeType", "application/octet-stream"),
            "_aevs_data_sha256": hashlib.sha256(decoded).hexdigest(),
            "_aevs_data_bytes": len(decoded),
        }

    if block_type == "resource":
        resource = getattr(block, "resource", None)
        uri = str(getattr(resource, "uri", "")) if resource else ""
        return {"type": "resource", "uri": uri}

    if block_type == "resource_link":
        uri = str(getattr(block, "uri", ""))
        return {"type": "resource_link", "uri": uri}

    return {"type": str(block_type), "_raw": str(block)[:500]}


def _serialize_call_tool_result(result: Any) -> Any:
    """Convert an MCP CallToolResult into a JSON-serialisable dict for AEVS receipts."""
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return {"structured": structured}

    content = getattr(result, "content", None)
    if content is None:
        return None

    serialised = [_serialize_content_block(block) for block in content]

    if len(serialised) == 1 and serialised[0].get("type") == "text":
        return serialised[0].get("text")

    return {"content": serialised}


def _extract_error_text(result: Any) -> str:
    """Pull the first text block from an error CallToolResult as the error message."""
    content = getattr(result, "content", None) or []
    for block in content:
        if getattr(block, "type", None) == "text":
            text = getattr(block, "text", None)
            if text is not None:
                return str(text)
    return "MCP tool returned isError=True"


def _is_task_result(result: Any) -> bool:
    """Return True if *result* is an MCP experimental CreateTaskResult."""
    return type(result).__name__ == "CreateTaskResult"


class MCPAdapter(BaseAdapter):
    """Patches ``mcp.client.session.ClientSession.call_tool`` to intercept
    every outbound MCP tool call and forward it to the AEVS receipt pipeline.

    MCP is async-only — there is no sync ``call_tool``.  The sync handler
    accepted by ``patch()`` is stored but never invoked.
    """

    def __init__(self) -> None:
        self._original_call_tool: Any = None
        self._patched = False

    @property
    def name(self) -> str:
        return "mcp"

    def is_available(self) -> bool:
        try:
            import mcp.client.session  # noqa: F401

            return _check_mcp_version()
        except ImportError:
            return False

    def patch(
        self,
        on_tool_call: ToolCallHandler,
        on_tool_call_async: AsyncToolCallHandler,
    ) -> None:
        if self._patched:
            return

        from mcp.client.session import ClientSession

        self._original_call_tool = ClientSession.call_tool
        mcp_version = _get_mcp_version()
        original_call_tool = self._original_call_tool

        @functools.wraps(original_call_tool)
        async def patched_call_tool(
            session_self: Any,
            name: str,
            arguments: dict[str, Any] | None = None,
            *args: Any,
            **kwargs: Any,
        ) -> Any:
            if _aevs_tracking_active.get(False):
                return await original_call_tool(session_self, name, arguments, *args, **kwargs)

            token = _aevs_tracking_active.set(True)
            tool_call_id = str(uuid.uuid4())
            inputs = arguments if arguments is not None else {}

            meta = kwargs.get("meta")
            run_id: str | None = None
            parent_run_id: str | None = None
            if isinstance(meta, dict):
                run_id = meta.get("run_id") or meta.get("trace_id")
                parent_run_id = meta.get("parent_run_id")

            started_at = datetime.now(timezone.utc)
            captured_exception: BaseException | None = None
            result: Any = None
            status = "success"
            error: str | None = None

            try:
                result = await original_call_tool(session_self, name, arguments, *args, **kwargs)
            except Exception as exc:
                status = "error"
                error = str(exc)
                captured_exception = exc
            finally:
                _aevs_tracking_active.reset(token)

            # Experimental Tasks return a task handle, not a tool result.
            # Skip receipt creation — see MCP_ADAPTER.md section 4.8.
            # Note: if the call raised, result is None so _is_task_result is
            # always False in that path; captured_exception is re-raised below.
            if result is not None and _is_task_result(result):
                logger.warning(
                    "AEVS: MCP tool %r returned CreateTaskResult — "
                    "receipt skipped (not yet supported)",
                    name,
                )
                return result

            # All post-call processing is wrapped in try/except to uphold
            # design rule #1: AEVS must NEVER crash the user's agent.
            output: Any = None
            try:
                if result is not None and getattr(result, "isError", False) and status != "error":
                    status = "error"
                    error = _extract_error_text(result)

                ended_at = datetime.now(timezone.utc)
                output = _serialize_call_tool_result(result) if result is not None else None
            except Exception:
                logger.debug("AEVS: failed to process MCP tool result", exc_info=True)
                ended_at = datetime.now(timezone.utc)

            try:
                await on_tool_call_async(
                    tool_name=name,
                    inputs=inputs,
                    output=output,
                    status=status,
                    error=error,
                    started_at=started_at,
                    ended_at=ended_at,
                    run_id=run_id,
                    parent_run_id=parent_run_id,
                    tool_call_id=tool_call_id,
                    framework="mcp",
                    framework_version=mcp_version,
                )
            except Exception:
                logger.debug("AEVS: failed to process MCP tool call", exc_info=True)

            if captured_exception is not None:
                raise captured_exception
            return result

        ClientSession.call_tool = patched_call_tool  # type: ignore[assignment]
        self._patched = True
        logger.info("AEVS: MCP adapter patched (ClientSession.call_tool)")

    def unpatch(self) -> None:
        if not self._patched:
            return

        from mcp.client.session import ClientSession

        ClientSession.call_tool = self._original_call_tool  # type: ignore[method-assign]
        self._original_call_tool = None
        self._patched = False
        logger.info("AEVS: MCP adapter unpatched")
