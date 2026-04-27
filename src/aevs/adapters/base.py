from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Coroutine
from contextvars import ContextVar
from typing import Any

# Callback type: called by adapters for every intercepted tool call.
# Sync version returns None, async version returns a coroutine.
ToolCallHandler = Callable[..., None]
AsyncToolCallHandler = Callable[..., Coroutine[Any, Any, None]]

# Cross-adapter deduplication flag.
#
# When multiple adapters are active (e.g. "langchain" + "mcp"), a single
# logical tool call can flow through both interception points — the outer
# LangChain BaseTool.invoke wraps an inner MCP ClientSession.call_tool.
# Without coordination, AEVS would emit two receipts for the same call.
#
# The FIRST adapter to intercept a call sets this ContextVar to True.
# Any adapter that sees it already True skips receipt creation and just
# forwards the call.  ContextVar propagates correctly through both sync
# threads and async await-chains (but NOT across asyncio.create_task
# boundaries, which is fine — a new task is a new logical call).
_aevs_tracking_active: ContextVar[bool] = ContextVar("_aevs_tracking_active", default=False)


class BaseAdapter(ABC):
    """Interface for framework-specific adapters.

    Each adapter patches one framework's tool dispatch to intercept calls
    and forward them to the handler functions provided by `patch()`.
    """

    @abstractmethod
    def patch(
        self,
        on_tool_call: ToolCallHandler,
        on_tool_call_async: AsyncToolCallHandler,
    ) -> None:
        """Monkey-patch the framework. Must be idempotent."""
        ...

    @abstractmethod
    def unpatch(self) -> None:
        """Restore original framework behavior. Must be idempotent."""
        ...

    @abstractmethod
    def is_available(self) -> bool:
        """Return True if the framework is importable."""
        ...

    @property
    @abstractmethod
    def name(self) -> str:
        ...
