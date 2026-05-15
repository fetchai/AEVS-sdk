"""Internal module wiring the public API: enable(), disable(), flush().

All heavy imports are deferred to function bodies so that `import aevs`
has zero side effects (design rule #8).
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import uuid
from collections import deque
from typing import Any

from aevs.config import get_config
from aevs.core.types import ReceiptPayload

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

# Active session UUIDv4 — set in ``enable()`` and cleared in ``disable()``.
# Surfaced by :func:`aevs.get_session_id` for log correlation.
_session_id: str | None = None

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
    global _consecutive_store_failures, _session_id
    _receipt_builder = None
    _client = None
    _buffer = None
    _drainer = None
    _adapters.clear()
    _enabled = False
    _session_id = None
    _consecutive_store_failures = 0
    _state_lock = threading.Lock()
    _registry_lock = threading.Lock()
    _reference_registry.clear()
    _reference_deque.clear()


try:
    os.register_at_fork(after_in_child=_after_fork_child)
except AttributeError:
    logger.debug("AEVS: os.register_at_fork unavailable (Windows); fork safety disabled")


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
        logger.debug("AEVS: langchain-mcp-adapters not installed, no bridge overlap possible")


def enable(*, frameworks: list[str] | None = None) -> None:
    """Detect installed frameworks, patch them, and start intercepting tool calls.

    If :func:`aevs.configure` was never called, or was called without a
    valid API key, ``enable()`` logs a warning and returns immediately —
    keeping the host agent running in no-op mode.
    """
    global _receipt_builder, _client, _buffer, _drainer, _enabled, _session_id

    with _state_lock:
        if _enabled:
            return

        from aevs._drainer import BufferDrainer
        from aevs.core.buffer import LocalBuffer
        from aevs.core.client import AEVSClient
        from aevs.core.receipt import ReceiptBuilder

        config = get_config()
        if config is None:
            return

        new_client: AEVSClient | None = None
        new_buffer: LocalBuffer | None = None

        try:
            new_client = AEVSClient(config)
            try:
                new_buffer = LocalBuffer(
                    config.buffer_path,
                    config.key_secret,
                    max_records=config.max_buffer_records,
                )
            except sqlite3.DatabaseError:
                # Buffer file exists but is unreadable as a SQLite DB
                # (corruption, partial write, foreign file at that path,
                # encryption-key mismatch surfacing here, etc.).  Recover
                # by purging and recreating once — never crash the host.
                logger.warning(
                    "AEVS: buffer file unreadable as SQLite DB at %s; "
                    "purging and starting fresh",
                    config.buffer_path,
                )
                resolved = config.buffer_path.expanduser().resolve()
                for suffix in ("", "-wal", "-shm"):
                    path = str(resolved) + suffix
                    try:
                        os.remove(path)
                        logger.debug("AEVS: removed corrupt buffer file %s", path)
                    except FileNotFoundError:
                        logger.debug("AEVS: buffer file %s not present, skipping", path)
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
                    logger.debug(
                        "AEVS: error closing client during buffer init rollback",
                        exc_info=True,
                    )
            logger.warning(
                "AEVS: failed to initialize buffer/client. "
                "AEVS will run in no-op mode — no receipts will be captured.",
                exc_info=True,
            )
            return

        # Session lifecycle gate — decides between three states based on
        # the buffer's persisted state and the count of un-flushed
        # (pending) receipts:
        #
        # 1. ``pending_count > 0`` *and* the persisted ``chain_state``
        #    carries a ``session_id``: we are recovering from a
        #    mid-session crash where receipts never reached the backend.
        #    Reuse the same ``session_id`` so the new receipts continue
        #    the chain the pending ones started; resume ``seq`` and
        #    ``prev_hash`` from ``chain_state``.  This keeps one linear
        #    chain across the crash boundary so the drainer ships a
        #    single, verifiable chain.
        #
        # 2. ``pending_count > 0`` but no persisted ``session_id`` (legacy
        #    buffer file written by an SDK that predates this column):
        #    we have unsent receipts whose session is unknown.  Mint a
        #    fresh ``session_id`` for new receipts but keep ``seq``/
        #    ``prev_hash`` continuity so the drainer can ship the legacy
        #    pending receipts and the new ones as a single hash-linked
        #    chain (different sessions, same hash linkage — backend
        #    accepts both because session_id is nullable).
        #
        # 3. ``pending_count == 0`` (fresh DB or clean drain): mint a
        #    fresh ``session_id`` and reset ``seq=0, prev_hash=None`` so
        #    the next receipt computes a brand-new session-scoped anchor.
        #    Two consecutive ``enable()`` cycles on the same buffer
        #    therefore occupy distinct chain spaces, eliminating the
        #    fork concept entirely (production audit issue #18 — solved
        #    by isolation rather than by stitching).
        #
        # If the buffer is unreadable for any reason (key rotation,
        # disk corruption, etc.) we purge and recreate — the SDK never
        # crashes the host agent.
        start_seq = 0
        last_prev_hash: str | None = None
        session_id: str = str(uuid.uuid4())
        try:
            persisted = new_buffer.chain_state()
            pending = new_buffer.pending_count()
            if pending > 0 and persisted is not None:
                persisted_seq, persisted_hash, persisted_session = persisted
                start_seq = persisted_seq
                last_prev_hash = persisted_hash
                if persisted_session is not None:
                    session_id = persisted_session
                    logger.info(
                        "AEVS: mid-session crash recovery — resuming "
                        "session_id=%s at seq=%d",
                        session_id,
                        start_seq,
                    )
                else:
                    logger.info(
                        "AEVS: legacy pending receipts present (no session_id) "
                        "— minting new session %s, hash chain stays linear "
                        "across the boundary",
                        session_id,
                    )
            elif pending > 0 and persisted is None:
                # Two distinct scenarios collapse onto this branch:
                #   1. legacy buffer file written by an SDK that predates
                #      the ``chain_state`` table (no row at all)
                #   2. buffer file written by a different API key — the
                #      key-fingerprint guard inside ``chain_state()``
                #      surfaces this as ``None``
                # Disambiguate by probing one decrypt: if it fails the
                # buffer is unreadable and we let the outer ``except``
                # purge it.
                new_buffer.last_receipt_bytes()
                from aevs.crypto.chain import compute_receipt_hash

                start_seq = new_buffer.max_seq()
                if start_seq > 0:
                    last_bytes = new_buffer.last_receipt_bytes()
                    if last_bytes is not None:
                        last_prev_hash = compute_receipt_hash(last_bytes)
                logger.info(
                    "AEVS: legacy pending receipts present (no chain_state) "
                    "— minting new session %s, resuming seq=%d so the drainer "
                    "ships one hash-linked chain across the version boundary",
                    session_id,
                    start_seq,
                )
            elif pending == 0 and persisted is not None:
                # Drop the prior session's chain_state row before the
                # new session writes its first receipt; store()'s
                # monotonic UPSERT guard would otherwise refuse to
                # overwrite it and a crash here would mis-route the
                # next enable() into recovery against the wrong session.
                new_buffer.reset_chain_state()
                logger.info(
                    "AEVS: clean drain detected — minting new session %s, "
                    "starting fresh chain",
                    session_id,
                )
        except Exception:
            logger.warning(
                "AEVS: could not read buffer state (key changed?), "
                "purging and starting fresh",
            )
            start_seq = 0
            last_prev_hash = None
            session_id = str(uuid.uuid4())
            new_buffer.close()
            resolved = config.buffer_path.expanduser().resolve()
            for suffix in ("", "-wal", "-shm"):
                path = str(resolved) + suffix
                try:
                    os.remove(path)
                    logger.debug("AEVS: removed stale buffer file %s", path)
                except FileNotFoundError:
                    logger.debug("AEVS: buffer file %s not present, skipping", path)
            new_buffer = LocalBuffer(
                config.buffer_path,
                config.key_secret,
                max_records=config.max_buffer_records,
            )

        new_builder = ReceiptBuilder(
            config,
            session_id=session_id,
            start_seq=start_seq,
            prev_hash=last_prev_hash,
        )

        frameworks_to_try = (
            frameworks if frameworks is not None else list(_ADAPTER_REGISTRY.keys())
        )
        new_adapters: list[Any] = []

        try:
            for name in frameworks_to_try:
                entry = _ADAPTER_REGISTRY.get(name)
                if entry is None:
                    logger.warning(
                        "AEVS: Unknown framework adapter: %r. Skipping.",
                        name,
                    )
                    continue

                module_path, class_name = entry
                try:
                    import importlib

                    mod = importlib.import_module(module_path)
                    adapter_cls = getattr(mod, class_name)
                except (ImportError, AttributeError) as exc:
                    logger.warning(
                        "AEVS: Failed to load adapter %r: %s. Skipping.",
                        name, exc,
                    )
                    continue

                adapter = adapter_cls()
                if adapter.is_available():
                    adapter.patch(
                        on_tool_call=_handle_tool_call,
                        on_tool_call_async=_handle_tool_call_async,
                    )
                    new_adapters.append(adapter)
                    logger.info("AEVS: enabled %s adapter", name)
                elif frameworks is not None:
                    logger.warning(
                        "AEVS: Framework %r requested but not installed. Skipping.",
                        name,
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
            logger.warning(
                "AEVS: unexpected error during adapter setup. "
                "AEVS will run in no-op mode — no receipts will be captured.",
                exc_info=True,
            )
            return

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
        _session_id = session_id
        _adapters.extend(new_adapters)
        _enabled = True
        new_drainer.start()


def disable() -> None:
    """Unpatch all frameworks and tear down runtime state.

    Stops the background drainer (performing a final synchronous flush),
    then closes the client and buffer.
    """
    global _receipt_builder, _client, _buffer, _drainer, _enabled, _session_id
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
        _session_id = None
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


def get_session_id() -> str | None:
    """Return the active session UUID, or ``None`` when AEVS is disabled.

    Each ``aevs.enable()`` call mints a UUIDv4 (or recovers a persisted
    one when there are unsent buffered receipts).  Every receipt produced
    during that session carries this id, and the chain anchor for the
    session is derived from it — making the chain cryptographically
    isolated from any other session that shares the same API key.

    Useful for log correlation ("which session crashed at 14:32?") and
    for support tooling that needs to look up a particular session's
    receipts on the backend without coordinating with the application.
    """
    return _session_id


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
        if config is None:
            return

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
            buffer.store(
                receipt["seq"],
                payload_bytes,
                receipt["prev_hash"],
                session_id=receipt.get("session_id"),
            )
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
        if config is None:
            return

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
            buffer.store(
                receipt["seq"],
                payload_bytes,
                receipt["prev_hash"],
                session_id=receipt.get("session_id"),
            )
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
    "run_id": str | None, "tool_call_id": str | None}``.

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
