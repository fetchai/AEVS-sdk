"""Internal module wiring the public API: enable(), disable(), flush().

All heavy imports are deferred to function bodies so that `import aevs`
has zero side effects (design rule #8).
"""

from __future__ import annotations

import logging
import os
import threading
from collections import deque
from typing import Any

from aevs.config import get_config
from aevs.core.types import ReceiptPayload
from aevs.exceptions import AEVSConfigError

logger = logging.getLogger("aevs")

# ---------------------------------------------------------------------------
# Runtime state (populated by enable(), cleared by disable())
# ---------------------------------------------------------------------------
_receipt_builder: Any = None
_client: Any = None
_buffer: Any = None
_drainer: Any = None
_adapters: list[Any] = []
_enabled: bool = False

_state_lock = threading.Lock()

_DEFAULT_MAX_REFS = 1_000

_reference_registry: dict[str, str] = {}
_reference_deque: deque[dict[str, str | int | None]] = deque(maxlen=_DEFAULT_MAX_REFS)
_registry_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Health tracking — counts consecutive buffer.store() failures so that
# callers can poll aevs.is_healthy() instead of scraping error logs.
# Only consecutive failures matter: a single transient hiccup is reset by
# the next successful write, keeping the signal noise-free.
#
# CPython GIL makes reading/writing a module-level int effectively atomic,
# so no additional lock is needed for this approximate counter.
# ---------------------------------------------------------------------------
_consecutive_store_failures: int = 0

_ADAPTER_REGISTRY: dict[str, tuple[str, str]] = {
    "langchain": ("aevs.adapters.langchain", "LangChainAdapter"),
    "mcp": ("aevs.adapters.mcp", "MCPAdapter"),
}


# ---------------------------------------------------------------------------
# Fork safety — inherited SQLite connections and httpx pools are invalid
# in the child process.  Reset state so the child starts clean.
# ---------------------------------------------------------------------------
def _after_fork_child() -> None:
    global _receipt_builder, _client, _buffer, _drainer, _enabled, _state_lock, _registry_lock
    global _consecutive_store_failures
    _receipt_builder = None
    _client = None
    _buffer = None
    _drainer = None
    _adapters.clear()
    _enabled = False
    _consecutive_store_failures = 0
    _state_lock = threading.Lock()
    _registry_lock = threading.Lock()
    _reference_registry.clear()
    _reference_deque.clear()


try:
    os.register_at_fork(after_in_child=_after_fork_child)
except AttributeError:
    pass  # Windows


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _warn_dual_mcp_langchain(adapters: list[Any]) -> None:
    """Emit a one-time warning when both MCP and LangChain adapters are active
    and langchain-mcp-adapters is installed — tool calls routed through that
    bridge would be intercepted by both adapters.

    The ``_aevs_tracking_active`` ContextVar prevents double-receipts at
    runtime, but the warning helps users understand the interaction and
    choose explicitly.
    """
    adapter_names = {a.name for a in adapters}
    if not ({"mcp", "langchain"} <= adapter_names):
        return
    try:
        import langchain_mcp_adapters  # type: ignore[import-not-found]  # noqa: F401

        logger.warning(
            "AEVS: both 'mcp' and 'langchain' adapters are active and "
            "langchain-mcp-adapters is installed. Tool calls through "
            "langchain-mcp-adapters will be intercepted by the outer "
            "adapter only (no double-counting) thanks to the "
            "_aevs_tracking_active guard. If this is intentional you can "
            "ignore this warning; otherwise enable only one adapter."
        )
    except ImportError:
        pass


def enable(*, frameworks: list[str] | None = None) -> None:
    """Detect installed frameworks, patch them, and start intercepting tool calls."""
    global _receipt_builder, _client, _buffer, _drainer, _enabled

    with _state_lock:
        if _enabled:
            return

        from aevs._drainer import BufferDrainer
        from aevs.core.buffer import LocalBuffer
        from aevs.core.client import AEVSClient
        from aevs.core.receipt import ReceiptBuilder

        config = get_config()

        new_client: AEVSClient | None = None
        new_buffer: LocalBuffer | None = None

        try:
            new_client = AEVSClient(config)
            new_buffer = LocalBuffer(
                config.buffer_path,
                config.key_secret,
                max_records=config.max_buffer_records,
            )
        except Exception:
            if new_client is not None:
                try:
                    new_client.close()
                except Exception:
                    pass
            raise

        # Resume seq and hash chain from any pending receipts left by a prior session.
        # If the buffer is unreadable (e.g. key changed), start fresh — never crash.
        start_seq = 0
        last_prev_hash: str | None = None
        try:
            start_seq = new_buffer.max_seq()
            if start_seq > 0:
                last_bytes = new_buffer.last_receipt_bytes()
                if last_bytes is not None:
                    from aevs.crypto.chain import compute_receipt_hash

                    last_prev_hash = compute_receipt_hash(last_bytes)
                logger.info(
                    "AEVS: resuming from buffer (last seq=%d)", start_seq
                )
        except Exception:
            logger.warning(
                "AEVS: could not read buffer state (key changed?), purging and starting fresh",
            )
            start_seq = 0
            last_prev_hash = None
            new_buffer.close()
            try:
                os.remove(config.buffer_path.expanduser().resolve())
            except FileNotFoundError:
                pass  # already gone — that's fine
            new_buffer = LocalBuffer(
                config.buffer_path,
                config.key_secret,
                max_records=config.max_buffer_records,
            )

        new_builder = ReceiptBuilder(
            config, start_seq=start_seq, prev_hash=last_prev_hash
        )

        frameworks_to_try = (
            frameworks if frameworks is not None else list(_ADAPTER_REGISTRY.keys())
        )
        new_adapters: list[Any] = []

        try:
            for name in frameworks_to_try:
                entry = _ADAPTER_REGISTRY.get(name)
                if entry is None:
                    raise AEVSConfigError(f"Unknown framework adapter: {name!r}")

                module_path, class_name = entry
                try:
                    import importlib

                    mod = importlib.import_module(module_path)
                    adapter_cls = getattr(mod, class_name)
                except (ImportError, AttributeError) as exc:
                    raise AEVSConfigError(
                        f"Failed to load adapter {name!r}: {exc}"
                    ) from exc

                adapter = adapter_cls()
                if adapter.is_available():
                    adapter.patch(
                        on_tool_call=_handle_tool_call,
                        on_tool_call_async=_handle_tool_call_async,
                    )
                    new_adapters.append(adapter)
                    logger.info("AEVS: enabled %s adapter", name)
                elif frameworks is not None:
                    raise AEVSConfigError(
                        f"Framework {name!r} requested but not installed"
                    )
        except Exception:
            for adapter in new_adapters:
                try:
                    adapter.unpatch()
                except Exception:
                    logger.debug(
                        "AEVS: error unpatching adapter during enable() rollback",
                        exc_info=True,
                    )
            try:
                new_client.close()
            except Exception:
                logger.debug("AEVS: error closing client during enable() rollback", exc_info=True)
            try:
                new_buffer.close()
            except Exception:
                logger.debug("AEVS: error closing buffer during enable() rollback", exc_info=True)
            raise

        _warn_dual_mcp_langchain(new_adapters)

        new_drainer = BufferDrainer(
            new_buffer, new_client, interval_ms=config.drain_interval_ms
        )

        # Publish to globals only after full success
        global _reference_deque
        with _registry_lock:
            _reference_deque = deque(maxlen=config.max_reference_entries)
            _reference_registry.clear()
        _receipt_builder = new_builder
        _client = new_client
        _buffer = new_buffer
        _drainer = new_drainer
        _adapters.extend(new_adapters)
        _enabled = True
        new_drainer.start()


def disable() -> None:
    """Unpatch all frameworks and tear down runtime state.

    Stops the background drainer (performing a final synchronous flush),
    then closes the client and buffer.
    """
    global _receipt_builder, _client, _buffer, _drainer, _enabled
    global _consecutive_store_failures

    with _state_lock:
        for adapter in _adapters:
            try:
                adapter.unpatch()
            except Exception:
                logger.debug("AEVS: error unpatching adapter", exc_info=True)
        _adapters.clear()

        if _drainer is not None:
            try:
                _drainer.stop(final_flush=True)
            except Exception:
                logger.debug("AEVS: error stopping drainer", exc_info=True)
        if _client is not None:
            try:
                _client.close()
            except Exception:
                logger.debug("AEVS: error closing HTTP client in disable()", exc_info=True)
        if _buffer is not None:
            try:
                _buffer.close()
            except Exception:
                logger.debug("AEVS: error closing buffer in disable()", exc_info=True)

        _receipt_builder = None
        _client = None
        _buffer = None
        _drainer = None
        _enabled = False
        _consecutive_store_failures = 0
        with _registry_lock:
            _reference_registry.clear()
            _reference_deque.clear()


def flush() -> None:
    """Send all buffered receipts to the backend. Blocks until done or failure."""
    with _state_lock:
        if not _enabled:
            return
        drainer = _drainer
    if drainer is None:
        return

    drainer.drain()


def is_healthy(*, threshold: int = 3) -> bool:
    """Return ``True`` when the SDK is operating normally.

    Returns ``False`` when ``buffer.store()`` has failed at least *threshold*
    consecutive times without a single success in between.  A transient I/O
    hiccup (one failure followed by a success) resets the counter, so this
    only fires for sustained problems such as a full disk or corrupted SQLite
    file.

    The SDK never crashes the agent regardless of this value — receipts are
    simply dropped while unhealthy.  Callers can wire this into an existing
    ``/health`` or ``/readiness`` HTTP endpoint to surface the problem to
    their monitoring infrastructure.

    Example::

        @app.get("/health")
        def health():
            if not aevs.is_healthy():
                return {"status": "degraded", "detail": "AEVS receipt buffer failing"}
            return {"status": "ok"}

    Args:
        threshold: Number of consecutive failures before the SDK is
            considered unhealthy.  Default is 3.
    """
    return _consecutive_store_failures < threshold



#
# CRITICAL: These functions must NEVER raise.  Any exception would propagate
# through the adapter's monkey-patch and crash the user's agent (design rule #1).
# ---------------------------------------------------------------------------


def _handle_tool_call(**kwargs: Any) -> None:
    """Sync handler: build receipt, write to local buffer.

    The background drainer sends buffered receipts to the backend
    asynchronously, so this function returns almost instantly.
    """
    with _state_lock:
        if not _enabled:
            return
        builder = _receipt_builder
        buffer = _buffer
        if builder is None or buffer is None:
            return

    global _consecutive_store_failures
    try:
        from aevs.core.serializer import canonical_json

        config = get_config()
        receipt: ReceiptPayload = builder.build(**kwargs)

        ref_id = receipt.get("reference_id")
        run_id = kwargs.get("run_id")
        tool_call_id = kwargs.get("tool_call_id")
        if ref_id:
            _record_reference(
                receipt["seq"], kwargs.get("tool_name", ""),
                ref_id, run_id, tool_call_id,
            )

        payload_bytes = canonical_json(
            receipt,
            float_handling=config.float_handling,
            float_precision=config.float_precision,
        )

        # Re-verify globals vs snapshots so disable() cannot interleave teardown
        # with a write to a closed buffer (see production audit: TOCTOU).
        with _state_lock:
            if not _enabled or _buffer is not buffer or _receipt_builder is not builder:
                return
            buffer.store(receipt["seq"], payload_bytes, receipt["prev_hash"])
        _consecutive_store_failures = 0
    except Exception:
        _consecutive_store_failures += 1
        logger.error("AEVS: unexpected error in tool call handler", exc_info=True)


async def _handle_tool_call_async(**kwargs: Any) -> None:
    """Async handler: build receipt, write to local buffer.

    Identical to the sync handler — the SQLite write is fast enough
    (~0.1 ms with WAL) that blocking the event loop briefly is acceptable.
    The background drainer sends receipts to the backend.
    """
    with _state_lock:
        if not _enabled:
            return
        builder = _receipt_builder
        buffer = _buffer
        if builder is None or buffer is None:
            return

    global _consecutive_store_failures
    try:
        from aevs.core.serializer import canonical_json

        config = get_config()
        receipt: ReceiptPayload = builder.build(**kwargs)

        ref_id = receipt.get("reference_id")
        run_id = kwargs.get("run_id")
        tool_call_id = kwargs.get("tool_call_id")
        if ref_id:
            _record_reference(
                receipt["seq"], kwargs.get("tool_name", ""),
                ref_id, run_id, tool_call_id,
            )

        payload_bytes = canonical_json(
            receipt,
            float_handling=config.float_handling,
            float_precision=config.float_precision,
        )

        with _state_lock:
            if not _enabled or _buffer is not buffer or _receipt_builder is not builder:
                return
            buffer.store(receipt["seq"], payload_bytes, receipt["prev_hash"])
        _consecutive_store_failures = 0
    except Exception:
        _consecutive_store_failures += 1
        logger.error(
            "AEVS: unexpected error in async tool call handler", exc_info=True
        )


# ---------------------------------------------------------------------------
# Reference ID registry — lets the orchestration layer correlate tool
# invocations with AEVS receipts without touching tool output.
#
# Uses a bounded deque (FIFO) so memory stays capped even if the caller
# forgets to clear.  The limit is set by ``max_reference_entries`` in
# ``aevs.configure()`` (default 1 000).
# ---------------------------------------------------------------------------


def _record_reference(
    seq: int,
    tool_name: str,
    reference_id: str,
    run_id: str | None,
    tool_call_id: str | None = None,
) -> None:
    """Append an entry to the deque, evicting the oldest if full."""
    with _registry_lock:
        if len(_reference_deque) == _reference_deque.maxlen:
            evicted = _reference_deque[0]
            old_run_id = evicted.get("run_id")
            if isinstance(old_run_id, str):
                _reference_registry.pop(old_run_id, None)
            old_tc_id = evicted.get("tool_call_id")
            if isinstance(old_tc_id, str):
                _reference_registry.pop(old_tc_id, None)
        _reference_deque.append({
            "seq": seq,
            "tool_name": tool_name,
            "reference_id": reference_id,
            "run_id": run_id,
            "tool_call_id": tool_call_id,
        })
        if run_id:
            _reference_registry[run_id] = reference_id
        if tool_call_id:
            _reference_registry[tool_call_id] = reference_id


def get_reference_id(lookup_id: str) -> str | None:
    """Return the reference_id for a run_id or tool_call_id, or None."""
    with _registry_lock:
        return _reference_registry.get(lookup_id)


def get_reference_ids(*, clear: bool = False) -> list[dict[str, str | int | None]]:
    """Return all reference entries recorded since the last clear.

    Each entry is ``{"seq": int, "tool_name": str, "reference_id": str,
    "run_id": str | None}``.

    If *clear* is True the internal registry is emptied after copying,
    which is the recommended pattern for per-request web applications.
    """
    with _registry_lock:
        snapshot = list(_reference_deque)
        if clear:
            _reference_registry.clear()
            _reference_deque.clear()
        return snapshot


def clear_reference_ids() -> None:
    """Drop all stored reference entries."""
    with _registry_lock:
        _reference_registry.clear()
        _reference_deque.clear()
