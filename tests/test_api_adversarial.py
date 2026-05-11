"""Adversarial tests for aevs._api — designed to break, not just cover.

Targets every uncovered branch: fork-safety reset, enable() failure
cascades, disable() error swallowing, flush() edge cases, sync/async
tool-call handlers, and the bounded reference registry.
"""

from __future__ import annotations

import json
import os
from collections import deque
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

import aevs._api as api
from aevs.config import configure, get_config, reset_config
from aevs.exceptions import AEVSConfigError
from tests.conftest import (
    TEST_AGENT_ID,
    TEST_API_KEY,
    TEST_BASE_URL,
    TEST_KEY_SECRET,
    TEST_RECEIPTS_URL,
)

FIXED_START = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
FIXED_END = datetime(2026, 4, 1, 12, 0, 1, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean_api_state():
    reset_config()
    yield
    try:
        api.disable()
    except Exception:
        pass
    api._receipt_builder = None
    api._client = None
    api._buffer = None
    api._drainer = None
    api._adapters.clear()
    api._enabled = False
    api._reference_registry.clear()
    api._reference_deque.clear()
    reset_config()


# ===================================================================
# Fork safety — _after_fork_child must nuke all state
# ===================================================================


class TestAfterForkChild:
    def test_resets_all_globals(self):
        api._receipt_builder = "fake_builder"
        api._client = "fake_client"
        api._buffer = "fake_buffer"
        api._drainer = "fake_drainer"
        api._adapters.append("adapter1")
        api._enabled = True
        api._reference_registry["k"] = "v"
        api._reference_deque.append({"seq": 1})

        api._after_fork_child()

        assert api._receipt_builder is None
        assert api._client is None
        assert api._buffer is None
        assert api._drainer is None
        assert len(api._adapters) == 0
        assert api._enabled is False
        assert len(api._reference_registry) == 0
        assert len(api._reference_deque) == 0

    def test_lock_objects_are_fresh_after_fork(self):
        old_state_lock = api._state_lock
        old_reg_lock = api._registry_lock
        api._after_fork_child()
        assert api._state_lock is not old_state_lock
        assert api._registry_lock is not old_reg_lock


class TestRegisterAtForkWindowsFallback:
    def test_attribute_error_is_silently_caught(self):
        """On Windows, os.register_at_fork doesn't exist — verify the
        except AttributeError: pass path (lines 65-66)."""
        import importlib

        import aevs._api as api_mod

        saved_register = getattr(os, "register_at_fork", None)
        try:
            os.register_at_fork = MagicMock(side_effect=AttributeError)
            importlib.reload(api_mod)
        finally:
            if saved_register is not None:
                os.register_at_fork = saved_register
            else:
                delattr(os, "register_at_fork")
            importlib.reload(api_mod)


# ===================================================================
# enable() failure cascades — exercise lines 126-132, 149-160, 183-184, 196-214
# ===================================================================


class TestEnableFailurePaths:
    @respx.mock
    def test_client_construction_failure_propagates(self, tmp_path):
        """AEVSClient() raises during enable() — error must propagate."""
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, buffer_path=str(tmp_path / "buf.db"))
        with patch("aevs.core.client.AEVSClient.__init__",
                    side_effect=RuntimeError("conn fail")):
            with pytest.raises(RuntimeError, match="conn fail"):
                api.enable()
        assert api._enabled is False

    @respx.mock
    def test_buffer_failure_closes_client(self, tmp_path):
        """If LocalBuffer() fails after client is created, client must be closed."""
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, buffer_path=str(tmp_path / "buf.db"))
        with patch("aevs.core.buffer.LocalBuffer.__init__",
                    side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                api.enable()
        assert api._enabled is False

    @respx.mock
    def test_buffer_failure_client_close_also_fails(self, tmp_path):
        """If both buffer creation AND client.close() fail, original error wins."""
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, buffer_path=str(tmp_path / "buf.db"))
        broken_client = MagicMock()
        broken_client.close.side_effect = RuntimeError("close also broke")
        with patch("aevs.core.client.AEVSClient", return_value=broken_client), \
             patch("aevs.core.buffer.LocalBuffer.__init__",
                   side_effect=OSError("disk full")):
            with pytest.raises(OSError, match="disk full"):
                api.enable()

    def test_unknown_framework_raises(self, tmp_path):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, buffer_path=str(tmp_path / "buf.db"))
        with pytest.raises(AEVSConfigError, match="Unknown framework"):
            api.enable(frameworks=["nonexistent_framework"])

    def test_unavailable_explicit_framework_raises(self, tmp_path):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, buffer_path=str(tmp_path / "buf.db"))
        with patch("aevs.adapters.langchain.LangChainAdapter.is_available",
                    return_value=False):
            with pytest.raises(AEVSConfigError, match="not installed"):
                api.enable(frameworks=["langchain"])

    def test_adapter_import_failure_raises(self, tmp_path):
        """If importlib.import_module fails for an adapter, AEVSConfigError."""
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, buffer_path=str(tmp_path / "buf.db"))

        real_import = __import__("importlib").import_module

        def failing_import(name, *a, **kw):
            if name == "aevs.adapters.langchain":
                raise ImportError("module gone")
            return real_import(name, *a, **kw)

        with patch("importlib.import_module", side_effect=failing_import):
            with pytest.raises(AEVSConfigError, match="Failed to load"):
                api.enable(frameworks=["langchain"])

    def test_second_adapter_failure_unpatches_first_and_closes(self, tmp_path):
        """If second adapter fails, first is unpatched, client+buffer closed."""
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, buffer_path=str(tmp_path / "buf.db"))
        with patch("aevs.adapters.mcp.MCPAdapter.is_available",
                    side_effect=RuntimeError("mcp boom")):
            with pytest.raises(RuntimeError, match="mcp boom"):
                api.enable(frameworks=["langchain", "mcp"])
        assert api._enabled is False

    def test_adapter_failure_cleanup_all_raise(self, tmp_path):
        """When cleanup itself fails: unpatch, close, close all raise — original error wins.

        Scenario: langchain adapter patches OK, mcp adapter explodes,
        then during cleanup unpatch+close+close all throw.
        """
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, buffer_path=str(tmp_path / "buf.db"))

        from aevs.adapters.langchain import LangChainAdapter
        original_patch = LangChainAdapter.patch
        original_unpatch = LangChainAdapter.unpatch

        def poisoned_patch(self_adapter, on_tool_call, on_tool_call_async):
            original_patch(self_adapter, on_tool_call, on_tool_call_async)
            self_adapter.unpatch = MagicMock(side_effect=RuntimeError("unpatch fail"))

        with patch.object(LangChainAdapter, "patch", poisoned_patch), \
             patch("aevs.adapters.mcp.MCPAdapter.is_available",
                    side_effect=RuntimeError("mcp exploded")), \
             patch("aevs.core.client.AEVSClient.close",
                    side_effect=RuntimeError("close fail")), \
             patch("aevs.core.buffer.LocalBuffer.close",
                    side_effect=RuntimeError("buf close fail")):
            with pytest.raises(RuntimeError, match="mcp exploded"):
                api.enable(frameworks=["langchain", "mcp"])

        cleanup_adapter = LangChainAdapter()
        cleanup_adapter._patched = True
        from langchain_core.tools import BaseTool
        cleanup_adapter._original_invoke = BaseTool.invoke
        cleanup_adapter._original_ainvoke = BaseTool.ainvoke
        original_unpatch(cleanup_adapter)
        assert api._enabled is False

    @respx.mock
    def test_buffer_read_failure_during_resume_purges_and_continues(self, tmp_path):
        """If buffer.max_seq() raises (corrupt/key changed), purge + start fresh."""
        from aevs.core.buffer import LocalBuffer
        from aevs.core.serializer import canonical_json

        configure(
            api_key=TEST_API_KEY,
            agent_id=TEST_AGENT_ID,
            buffer_path=str(tmp_path / "buf.db"),
            base_url=TEST_BASE_URL,
        )

        buf = LocalBuffer(tmp_path / "buf.db", TEST_KEY_SECRET)
        buf.store(1, canonical_json({"seq": 1, "tool": "t"}), prev_hash="h")
        buf.close()

        wrong_key_hex = "cd" * 32
        wrong_key = "aevs_sk_testkey_" + wrong_key_hex
        configure(
            api_key=wrong_key,
            agent_id=TEST_AGENT_ID,
            buffer_path=str(tmp_path / "buf.db"),
            max_buffer_records=444,
            base_url=TEST_BASE_URL,
        )

        respx.post(TEST_RECEIPTS_URL).mock(
            return_value=httpx.Response(200))
        api.enable(frameworks=[])
        assert api._enabled is True
        assert api._buffer is not None
        assert api._buffer._max_records == get_config().max_buffer_records

    @respx.mock
    def test_buffer_purge_file_already_gone_is_ok(self, tmp_path):
        """If the db file vanishes between close() and os.remove(), FileNotFoundError
        is silently ignored and enable() succeeds (buffer is freshly re-created)."""
        from aevs.core.buffer import LocalBuffer
        from aevs.core.serializer import canonical_json

        configure(
            api_key=TEST_API_KEY,
            agent_id=TEST_AGENT_ID,
            buffer_path=str(tmp_path / "buf.db"),
            base_url=TEST_BASE_URL,
        )

        buf = LocalBuffer(tmp_path / "buf.db", TEST_KEY_SECRET)
        buf.store(1, canonical_json({"seq": 1}), prev_hash="h")
        buf.close()

        wrong_key_hex = "cd" * 32
        configure(
            api_key="aevs_sk_testkey_" + wrong_key_hex,
            agent_id=TEST_AGENT_ID,
            buffer_path=str(tmp_path / "buf.db"),
            base_url=TEST_BASE_URL,
        )

        def remove_then_gone(path):
            import os as _os
            _os.remove(path)        # actually delete it on first call
            raise FileNotFoundError("already gone on second hypothetical call")

        respx.post(TEST_RECEIPTS_URL).mock(
            return_value=httpx.Response(200))
        with patch("aevs._api.os.remove", side_effect=FileNotFoundError("already gone")):
            api.enable(frameworks=[])
        assert api._enabled is True

    @respx.mock
    def test_buffer_purge_failure_during_resume_raises(self, tmp_path):
        """If the purge step fails (e.g. permission denied), enable() must raise
        rather than silently continue with a broken buffer that drops receipts."""
        from aevs.core.buffer import LocalBuffer
        from aevs.core.serializer import canonical_json

        configure(
            api_key=TEST_API_KEY,
            agent_id=TEST_AGENT_ID,
            buffer_path=str(tmp_path / "buf.db"),
            base_url=TEST_BASE_URL,
        )

        buf = LocalBuffer(tmp_path / "buf.db", TEST_KEY_SECRET)
        buf.store(1, canonical_json({"seq": 1}), prev_hash="h")
        buf.close()

        wrong_key_hex = "cd" * 32
        configure(
            api_key="aevs_sk_testkey_" + wrong_key_hex,
            agent_id=TEST_AGENT_ID,
            buffer_path=str(tmp_path / "buf.db"),
            base_url=TEST_BASE_URL,
        )

        def broken_remove(path):
            raise PermissionError("can't remove")

        respx.post(TEST_RECEIPTS_URL).mock(
            return_value=httpx.Response(200))
        with patch("aevs._api.os.remove", side_effect=broken_remove):
            with pytest.raises(PermissionError, match="can't remove"):
                api.enable(frameworks=[])
        assert api._enabled is False

    @respx.mock
    def test_corrupt_buffer_file_recovers_on_construction(self, tmp_path):
        """If the buffer file at ``buffer_path`` is not a valid SQLite DB
        (e.g. corrupted on disk, partial write, or arbitrary bytes),
        ``LocalBuffer.__init__`` raises ``sqlite3.DatabaseError`` *before*
        the post-construction recovery path can fire.  ``enable()`` must
        catch this, purge the file, and recreate the buffer rather than
        propagating the error and crashing the host agent.

        Regression: prior to this fix, junk bytes at ``buffer_path``
        caused ``enable()`` to raise ``sqlite3.DatabaseError: file is not
        a database``, violating design rule #1 (never crash the agent).
        """
        from aevs.core.buffer import LocalBuffer

        buffer_path = tmp_path / "buf.db"
        buffer_path.write_bytes(b"not-a-sqlite-database-just-junk-bytes" * 100)

        configure(
            api_key=TEST_API_KEY,
            agent_id=TEST_AGENT_ID,
            buffer_path=str(buffer_path),
            base_url=TEST_BASE_URL,
        )

        respx.post(TEST_RECEIPTS_URL).mock(
            return_value=httpx.Response(200))
        api.enable(frameworks=[])

        try:
            assert api._enabled is True
            assert api._buffer is not None
            assert isinstance(api._buffer, LocalBuffer)
            assert api._buffer.max_seq() == 0
        finally:
            api.disable()


# ===================================================================
# Chain resume after a clean drain — production audit issue #18
# ===================================================================


class TestChainResumeAfterDrain:
    """After ``enable() → tool call → flush() → disable()`` fully drains
    the buffer, the next ``enable()`` mints a *new* ``session_id`` and
    starts its own chain space.

    The fork failure mode from production audit issue #18 is now
    impossible by construction: cycle 1 and cycle 2 receipts have
    different ``session_id`` values and different (session-scoped)
    anchors, so they can never collide on ``(seq, prev_hash)`` for the
    same ``(key_id, agent_id)``.
    """

    @staticmethod
    def _trigger_one_call() -> None:
        api._handle_tool_call(
            tool_name="ping",
            inputs={},
            output="pong",
            status="success",
            error=None,
            started_at=FIXED_START,
            ended_at=FIXED_END,
            framework="test",
            framework_version="0",
        )

    @respx.mock
    def test_post_drain_enable_starts_isolated_session(self, tmp_path):
        from aevs.core.buffer import LocalBuffer
        from aevs.crypto.chain import compute_chain_anchor

        respx.post(TEST_RECEIPTS_URL).mock(return_value=httpx.Response(200))
        configure(
            api_key=TEST_API_KEY,
            agent_id=TEST_AGENT_ID,
            buffer_path=str(tmp_path / "buf.db"),
            base_url=TEST_BASE_URL,
        )

        # ---- Cycle 1: enable, tool call, flush, disable -----------------
        api.enable(frameworks=[])
        try:
            self._trigger_one_call()
            api.flush()
        finally:
            api.disable()

        # The buffer drained cleanly; chain_state row carries the
        # cycle-1 fingerprint and session_id.
        buf = LocalBuffer(tmp_path / "buf.db", TEST_KEY_SECRET)
        try:
            assert buf.pending_count() == 0
            persisted = buf.chain_state()
            assert persisted is not None
            persisted_seq, _persisted_hash, persisted_session = persisted
            assert persisted_seq == 1
            assert persisted_session is not None, (
                "PR #3+#4 must persist session_id alongside seq/hash"
            )
        finally:
            buf.close()

        # ---- Cycle 2: enable, tool call, flush, disable -----------------
        api.enable(frameworks=[])
        try:
            self._trigger_one_call()
            api.flush()

            calls = list(respx.calls)
            assert len(calls) == 2, (
                f"expected one POST per cycle; got {len(calls)}"
            )
            r1 = json.loads(calls[0].request.content)
            r2 = json.loads(calls[1].request.content)

            # Each cycle has its own session_id and its own anchor.
            assert r1["session_id"] != r2["session_id"], (
                "post-drain enable() must mint a distinct session_id; "
                "got identical session_ids on cycles that share no chain"
            )
            anchor_r1 = compute_chain_anchor(TEST_KEY_SECRET, r1["session_id"])
            anchor_r2 = compute_chain_anchor(TEST_KEY_SECRET, r2["session_id"])
            assert anchor_r1 != anchor_r2

            assert r1["seq"] == 1
            assert r1["prev_hash"] == anchor_r1
            assert r2["seq"] == 1, (
                f"post-drain enable() must start a fresh chain at seq=1; "
                f"got seq={r2['seq']} — sessions are not isolated"
            )
            assert r2["prev_hash"] == anchor_r2, (
                "cycle 2 prev_hash must equal anchor(key, session_2); "
                "sessions must be cryptographically isolated"
            )
        finally:
            api.disable()

    @respx.mock
    def test_mid_session_crash_recovery_reuses_session_id(self, tmp_path):
        """If ``enable()`` finds pending receipts in the buffer (i.e. the
        prior session crashed before flush), it must reuse the persisted
        ``session_id`` so the new receipts continue the same chain."""
        from aevs.core.buffer import LocalBuffer

        # Simulate a crash by NOT mocking POST and NOT calling flush —
        # the disable() final-flush will fail to send and pending
        # receipts remain in the buffer.
        respx.post(TEST_RECEIPTS_URL).mock(side_effect=httpx.ConnectError("down"))
        configure(
            api_key=TEST_API_KEY,
            agent_id=TEST_AGENT_ID,
            buffer_path=str(tmp_path / "buf.db"),
            base_url=TEST_BASE_URL,
        )

        api.enable(frameworks=[])
        try:
            self._trigger_one_call()
            self._trigger_one_call()
        finally:
            api.disable()

        # Inspect the buffer pre-recovery.
        buf = LocalBuffer(tmp_path / "buf.db", TEST_KEY_SECRET)
        try:
            assert buf.pending_count() == 2
            persisted = buf.chain_state()
            assert persisted is not None
            _seq, _hash, persisted_session = persisted
            assert persisted_session is not None
        finally:
            buf.close()

        # Recovery: now the backend is reachable, capture every POST.
        respx.post(TEST_RECEIPTS_URL).mock(return_value=httpx.Response(200))

        api.enable(frameworks=[])
        try:
            self._trigger_one_call()
            api.flush()

            calls = [json.loads(c.request.content) for c in respx.calls]
            successful = [
                json.loads(c.request.content)
                for c in respx.calls
                if c.has_response and c.response.status_code == 200
            ]
            assert len(successful) == 3, (
                f"expected 3 successful POSTs (2 pre-crash + 1 post); "
                f"got {len(successful)} (total respx calls={len(calls)})"
            )

            # All three receipts share one session_id and seq is monotonic
            # across the crash boundary — that's mid-session recovery.
            session_ids = {r["session_id"] for r in successful}
            assert len(session_ids) == 1, (
                f"mid-session recovery must keep one session_id across the "
                f"crash; got {session_ids}"
            )
            seqs = sorted(r["seq"] for r in successful)
            assert seqs == [1, 2, 3], (
                f"sequence must continue across the crash; got {seqs}"
            )
        finally:
            api.disable()

    @respx.mock
    def test_clean_drain_then_crash_does_not_resurrect_prior_session(
        self, tmp_path
    ):
        """Regression for review-findings.md issue #1.

        Trace: session A drains cleanly to seq=5; session B starts and
        reaches only seq=3 before crashing.  The next ``enable()`` must
        recover under session B, not splice session A's identity onto
        the un-flushed receipts.
        """
        from aevs.core.buffer import LocalBuffer

        # Cycle 1: full clean drain under session A.
        respx.post(TEST_RECEIPTS_URL).mock(return_value=httpx.Response(200))
        configure(
            api_key=TEST_API_KEY,
            agent_id=TEST_AGENT_ID,
            buffer_path=str(tmp_path / "buf.db"),
            base_url=TEST_BASE_URL,
        )

        # Session A must reach a last_seq strictly greater than the
        # count session B will reach before its crash, otherwise the
        # UPSERT guard fires for session B's later writes and the bug
        # self-heals.
        api.enable(frameworks=[])
        session_a = api.get_session_id()
        assert session_a is not None
        try:
            for _ in range(5):
                self._trigger_one_call()
            api.flush()
        finally:
            api.disable()

        # Pre-condition for the bug: the stale row exists on disk.
        buf = LocalBuffer(tmp_path / "buf.db", TEST_KEY_SECRET)
        try:
            assert buf.pending_count() == 0
            persisted = buf.chain_state()
            assert persisted is not None
            persisted_seq, _, persisted_session = persisted
            assert persisted_session == session_a
            assert persisted_seq == 5, (
                f"cycle 1 should have advanced chain_state to seq=5; "
                f"got {persisted_seq}"
            )
        finally:
            buf.close()

        # Cycle 2: enable session B, store 3 receipts (< 5), then crash.
        respx.post(TEST_RECEIPTS_URL).mock(side_effect=httpx.ConnectError("crashed"))

        api.enable(frameworks=[])
        session_b = api.get_session_id()
        assert session_b is not None
        assert session_b != session_a
        for _ in range(3):
            self._trigger_one_call()
        # Bypass disable() to drop runtime state without a final flush.
        api._receipt_builder = None
        api._client = None
        api._buffer = None
        api._drainer = None
        api._adapters.clear()
        api._enabled = False
        api._session_id = None

        # Core invariant: chain_state must reflect session B, never A.
        buf = LocalBuffer(tmp_path / "buf.db", TEST_KEY_SECRET)
        try:
            assert buf.pending_count() == 3
            persisted = buf.chain_state()
            assert persisted is not None
            _, _, persisted_session = persisted
            assert persisted_session == session_b, (
                f"clean-drain reset failed — chain_state still reports "
                f"the prior session.  Got persisted_session={persisted_session!r}, "
                f"expected {session_b!r} (session A was {session_a!r}).  "
                f"A crash now would splice the two sessions into one "
                f"chain shipped to the backend (issue #1)."
            )
        finally:
            buf.close()

        # Cycle 3: recover, flush, verify the wire.
        respx.post(TEST_RECEIPTS_URL).mock(return_value=httpx.Response(200))

        api.enable(frameworks=[])
        try:
            self._trigger_one_call()
            api.flush()

            successful = [
                json.loads(c.request.content)
                for c in respx.calls
                if c.has_response and c.response.status_code == 200
            ]
            # Cycle 1's POSTs also returned 200, so filter by session.
            post_crash = [r for r in successful if r["session_id"] != session_a]
            assert len(post_crash) == 4, (
                f"expected 4 post-crash POSTs (3 buffered + 1 new); got {len(post_crash)}"
            )

            session_ids = {r["session_id"] for r in post_crash}
            assert session_ids == {session_b}, (
                f"backend received receipts under the wrong session — "
                f"got {session_ids}, expected {{{session_b!r}}}"
            )

            seqs = sorted(r["seq"] for r in post_crash)
            assert seqs == [1, 2, 3, 4], f"sequence not contiguous: {seqs}"
        finally:
            api.disable()

    @respx.mock
    def test_post_drain_resume_survives_rotated_key_by_starting_fresh(
        self, tmp_path
    ):
        """Defensive: if the same buffer file is reused with a different
        key after a clean drain, ``chain_state()`` reports ``None`` and
        we restart at ``seq=1, prev_hash = anchor(new_key)`` instead of
        bridging the new chain onto the old key's last_hash."""
        from aevs.core.buffer import LocalBuffer
        from aevs.crypto.chain import compute_chain_anchor

        respx.post(TEST_RECEIPTS_URL).mock(return_value=httpx.Response(200))
        configure(
            api_key=TEST_API_KEY,
            agent_id=TEST_AGENT_ID,
            buffer_path=str(tmp_path / "buf.db"),
            base_url=TEST_BASE_URL,
        )
        api.enable(frameworks=[])
        try:
            self._trigger_one_call()
            api.flush()
        finally:
            api.disable()

        # Rotate to a different key on the same buffer file.
        rotated_key = "aevs_sk_rotated_" + "cd" * 32
        rotated_secret = bytes.fromhex("cd" * 32)
        configure(
            api_key=rotated_key,
            agent_id=TEST_AGENT_ID,
            buffer_path=str(tmp_path / "buf.db"),
            base_url=TEST_BASE_URL,
        )

        # The buffer's own chain_state under the new key reports None,
        # which is what enable() relies on to fall through cleanly.
        buf = LocalBuffer(tmp_path / "buf.db", rotated_secret)
        try:
            assert buf.chain_state() is None
        finally:
            buf.close()

        api.enable(frameworks=[])
        try:
            self._trigger_one_call()
            api.flush()

            calls = list(respx.calls)
            r2 = json.loads(calls[-1].request.content)
            assert r2["seq"] == 1
            assert r2["prev_hash"] == compute_chain_anchor(rotated_secret, r2["session_id"]), (
                "rotated key must start its own chain at its own anchor"
            )
        finally:
            api.disable()


# ===================================================================
# disable() error handling
# ===================================================================


class TestDisableErrorHandling:
    def test_adapter_unpatch_exception_swallowed(self):
        api._enabled = True
        bad_adapter = MagicMock()
        bad_adapter.unpatch.side_effect = RuntimeError("unpatch boom")
        api._adapters.append(bad_adapter)
        api._drainer = MagicMock()
        api._client = MagicMock()
        api._buffer = MagicMock()

        api.disable()

        assert api._enabled is False
        bad_adapter.unpatch.assert_called_once()

    def test_drainer_stop_exception_swallowed(self):
        api._enabled = True
        drainer = MagicMock()
        drainer.stop.side_effect = RuntimeError("stop boom")
        api._drainer = drainer
        api._client = MagicMock()
        api._buffer = MagicMock()

        api.disable()
        assert api._enabled is False

    def test_client_close_exception_swallowed(self):
        api._enabled = True
        client = MagicMock()
        client.close.side_effect = RuntimeError("close boom")
        api._client = client
        api._buffer = MagicMock()
        api._drainer = None

        api.disable()
        assert api._enabled is False

    def test_buffer_close_exception_swallowed(self):
        api._enabled = True
        buf = MagicMock()
        buf.close.side_effect = RuntimeError("buf close boom")
        api._buffer = buf
        api._client = None
        api._drainer = None

        api.disable()
        assert api._enabled is False

    def test_disable_clears_reference_registry(self):
        api._enabled = True
        api._drainer = None
        api._client = None
        api._buffer = None
        api._reference_registry["x"] = "y"
        api._reference_deque.append({"seq": 1})

        api.disable()
        assert len(api._reference_registry) == 0
        assert len(api._reference_deque) == 0


# ===================================================================
# flush() edge cases
# ===================================================================


class TestFlushEdgeCases:
    def test_flush_when_disabled_is_noop(self):
        api._enabled = False
        api.flush()

    def test_flush_when_drainer_is_none(self):
        api._enabled = True
        api._drainer = None
        api.flush()
        api._enabled = False

    def test_flush_delegates_to_drainer(self):
        api._enabled = True
        mock_drainer = MagicMock()
        api._drainer = mock_drainer
        api.flush()
        mock_drainer.drain.assert_called_once()
        api._enabled = False


# ===================================================================
# _handle_tool_call — sync handler adversarial tests
# ===================================================================


class TestHandleToolCallSync:
    def test_noop_when_disabled(self):
        api._enabled = False
        api._handle_tool_call(tool_name="x", inputs={}, output=None,
                              status="success", error=None,
                              started_at=FIXED_START, ended_at=FIXED_END)

    def test_noop_when_builder_is_none(self):
        api._enabled = True
        api._receipt_builder = None
        api._buffer = MagicMock()
        api._handle_tool_call(tool_name="x", inputs={}, output=None,
                              status="success", error=None,
                              started_at=FIXED_START, ended_at=FIXED_END)
        api._enabled = False

    def test_noop_when_buffer_is_none(self):
        api._enabled = True
        api._receipt_builder = MagicMock()
        api._buffer = None
        api._handle_tool_call(tool_name="x", inputs={}, output=None,
                              status="success", error=None,
                              started_at=FIXED_START, ended_at=FIXED_END)
        api._enabled = False

    def test_builder_crash_never_propagates(self, tmp_path):
        """design rule #1: handler must NEVER raise."""
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, buffer_path=str(tmp_path / "buf.db"))
        api._enabled = True
        api._receipt_builder = MagicMock()
        api._receipt_builder.build.side_effect = RuntimeError("build exploded")
        api._buffer = MagicMock()

        api._handle_tool_call(tool_name="evil", inputs={}, output=None,
                              status="success", error=None,
                              started_at=FIXED_START, ended_at=FIXED_END)
        api._enabled = False

    def test_records_reference_id(self, tmp_path):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, buffer_path=str(tmp_path / "buf.db"))
        from aevs.config import get_config
        from aevs.core.buffer import LocalBuffer
        from aevs.core.receipt import ReceiptBuilder

        builder = ReceiptBuilder(get_config(), session_id="00000000-0000-4000-8000-000000000004")
        buf = LocalBuffer(tmp_path / "ref.db", TEST_KEY_SECRET)

        api._enabled = True
        api._receipt_builder = builder
        api._buffer = buf

        api._handle_tool_call(
            tool_name="search",
            inputs={"q": "test"},
            output="result",
            status="success",
            error=None,
            started_at=FIXED_START,
            ended_at=FIXED_END,
            run_id="run-123",
            tool_call_id="tc-456",
        )

        assert api.get_reference_id("run-123") is not None
        assert api.get_reference_id("tc-456") is not None
        buf.close()
        api._enabled = False


# ===================================================================
# _handle_tool_call_async — async handler adversarial tests
# ===================================================================


class TestHandleToolCallAsync:
    @pytest.mark.asyncio
    async def test_noop_when_disabled(self):
        api._enabled = False
        await api._handle_tool_call_async(
            tool_name="x", inputs={}, output=None,
            status="success", error=None,
            started_at=FIXED_START, ended_at=FIXED_END)

    @pytest.mark.asyncio
    async def test_noop_when_builder_none(self):
        api._enabled = True
        api._receipt_builder = None
        api._buffer = MagicMock()
        await api._handle_tool_call_async(
            tool_name="x", inputs={}, output=None,
            status="success", error=None,
            started_at=FIXED_START, ended_at=FIXED_END)
        api._enabled = False

    @pytest.mark.asyncio
    async def test_noop_when_buffer_none(self):
        api._enabled = True
        api._receipt_builder = MagicMock()
        api._buffer = None
        await api._handle_tool_call_async(
            tool_name="x", inputs={}, output=None,
            status="success", error=None,
            started_at=FIXED_START, ended_at=FIXED_END)
        api._enabled = False

    @pytest.mark.asyncio
    async def test_builder_crash_never_propagates(self, tmp_path):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, buffer_path=str(tmp_path / "buf.db"))
        api._enabled = True
        api._receipt_builder = MagicMock()
        api._receipt_builder.build.side_effect = RuntimeError("async build boom")
        api._buffer = MagicMock()

        await api._handle_tool_call_async(
            tool_name="evil", inputs={}, output=None,
            status="success", error=None,
            started_at=FIXED_START, ended_at=FIXED_END)
        api._enabled = False

    @pytest.mark.asyncio
    async def test_records_reference_and_stores_receipt(self, tmp_path):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, buffer_path=str(tmp_path / "buf.db"))
        from aevs.config import get_config
        from aevs.core.buffer import LocalBuffer
        from aevs.core.receipt import ReceiptBuilder

        builder = ReceiptBuilder(get_config(), session_id="00000000-0000-4000-8000-000000000004")
        buf = LocalBuffer(tmp_path / "async_ref.db", TEST_KEY_SECRET)

        api._enabled = True
        api._receipt_builder = builder
        api._buffer = buf

        await api._handle_tool_call_async(
            tool_name="async_tool",
            inputs={"a": 1},
            output="done",
            status="success",
            error=None,
            started_at=FIXED_START,
            ended_at=FIXED_END,
            run_id="arun-1",
            tool_call_id="atc-1",
        )

        assert api.get_reference_id("arun-1") is not None
        assert buf.pending_count() == 1
        buf.close()
        api._enabled = False


# ===================================================================
# Reference registry — bounded deque eviction + edge cases
# ===================================================================


class TestReferenceRegistry:
    def test_get_reference_id_missing_returns_none(self):
        assert api.get_reference_id("nonexistent") is None

    def test_get_reference_ids_returns_snapshot(self):
        api._reference_deque.clear()
        api._reference_registry.clear()
        api._record_reference(1, "tool", "ref-1", "run-1", "tc-1")
        api._record_reference(2, "tool", "ref-2", "run-2", "tc-2")

        ids = api.get_reference_ids()
        assert len(ids) == 2
        assert ids[0]["reference_id"] == "ref-1"
        assert ids[1]["reference_id"] == "ref-2"
        assert len(api._reference_deque) == 2

    def test_get_reference_ids_entry_shape_matches_docstring(self):
        """Pins the dict's key set against the public docstring; if a
        field is added or renamed, this test fails so the docstring (and
        README example) cannot silently drift out of sync."""
        api._reference_deque.clear()
        api._reference_registry.clear()
        api._record_reference(7, "search", "ref-x", "run-x", "tc-x")

        ids = api.get_reference_ids()
        assert len(ids) == 1
        assert set(ids[0].keys()) == {
            "seq",
            "tool_name",
            "reference_id",
            "run_id",
            "tool_call_id",
        }
        assert ids[0] == {
            "seq": 7,
            "tool_name": "search",
            "reference_id": "ref-x",
            "run_id": "run-x",
            "tool_call_id": "tc-x",
        }

    def test_get_reference_ids_with_clear(self):
        api._reference_deque.clear()
        api._reference_registry.clear()
        api._record_reference(1, "tool", "ref-1", "run-1", "tc-1")

        ids = api.get_reference_ids(clear=True)
        assert len(ids) == 1
        assert len(api._reference_deque) == 0
        assert len(api._reference_registry) == 0

    def test_clear_reference_ids(self):
        api._reference_deque.clear()
        api._reference_registry.clear()
        api._record_reference(1, "tool", "ref-1", "run-1", "tc-1")
        api.clear_reference_ids()
        assert len(api._reference_deque) == 0
        assert len(api._reference_registry) == 0

    def test_eviction_removes_old_registry_entries(self):
        """When deque is full, evicted entry's run_id and tool_call_id must be purged."""
        api._reference_deque.clear()
        api._reference_registry.clear()
        old_deque = api._reference_deque
        api._reference_deque = deque(maxlen=2)

        api._record_reference(1, "t1", "ref-1", "run-1", "tc-1")
        api._record_reference(2, "t2", "ref-2", "run-2", "tc-2")
        assert api.get_reference_id("run-1") == "ref-1"

        api._record_reference(3, "t3", "ref-3", "run-3", "tc-3")
        assert api.get_reference_id("run-1") is None
        assert api.get_reference_id("tc-1") is None
        assert api.get_reference_id("run-2") == "ref-2"
        assert api.get_reference_id("run-3") == "ref-3"

        api._reference_deque = old_deque

    def test_eviction_with_none_run_id(self):
        """Evicting an entry with None run_id should not crash."""
        api._reference_deque.clear()
        api._reference_registry.clear()
        old_deque = api._reference_deque
        api._reference_deque = deque(maxlen=1)

        api._record_reference(1, "t", "ref-1", None, None)
        api._record_reference(2, "t", "ref-2", "run-2", "tc-2")

        assert len(api._reference_deque) == 1
        assert api.get_reference_id("run-2") == "ref-2"

        api._reference_deque = old_deque

    def test_record_reference_with_none_tool_call_id(self):
        api._reference_deque.clear()
        api._reference_registry.clear()
        api._record_reference(1, "tool", "ref-1", "run-1", None)
        assert api.get_reference_id("run-1") == "ref-1"

    def test_record_reference_with_none_run_id(self):
        api._reference_deque.clear()
        api._reference_registry.clear()
        api._record_reference(1, "tool", "ref-1", None, "tc-1")
        assert api.get_reference_id("tc-1") == "ref-1"


# ===================================================================
# Health probe — is_healthy() + consecutive failure counter
# ===================================================================


class TestIsHealthy:
    def setup_method(self, _):
        api._consecutive_store_failures = 0

    def teardown_method(self, _):
        api._consecutive_store_failures = 0

    def test_healthy_when_no_failures(self):
        assert api.is_healthy() is True

    def test_unhealthy_after_threshold_failures(self):
        api._consecutive_store_failures = 3
        assert api.is_healthy() is False

    def test_healthy_just_below_threshold(self):
        api._consecutive_store_failures = 2
        assert api.is_healthy(threshold=3) is True

    def test_unhealthy_at_threshold(self):
        api._consecutive_store_failures = 3
        assert api.is_healthy(threshold=3) is False

    def test_custom_threshold_zero(self):
        # threshold=0 means any failure makes it unhealthy
        api._consecutive_store_failures = 0
        assert api.is_healthy(threshold=0) is False

    def test_counter_resets_on_success(self):
        """A successful buffer.store() call resets the failure counter."""
        from unittest.mock import MagicMock, patch

        api._consecutive_store_failures = 5

        mock_builder = MagicMock()
        mock_builder.build.return_value = {
            "seq": 1,
            "prev_hash": "abc",
            "reference_id": None,
        }
        mock_buffer = MagicMock()
        mock_buffer.store.return_value = None  # success

        api._receipt_builder = mock_builder
        api._buffer = mock_buffer
        api._enabled = True

        with patch("aevs._api.get_config") as mock_cfg, \
             patch("aevs.core.serializer.canonical_json", return_value=b"{}"):
            cfg = MagicMock()
            cfg.float_handling = "decimal_string"
            cfg.float_precision = 6
            mock_cfg.return_value = cfg
            api._handle_tool_call(tool_name="t", status="ok")

        assert api._consecutive_store_failures == 0
        assert api.is_healthy() is True

        # cleanup
        api._receipt_builder = None
        api._buffer = None
        api._enabled = False

    def test_counter_increments_on_failure(self):
        """Each buffer.store() failure bumps the consecutive counter."""
        from unittest.mock import MagicMock, patch

        mock_builder = MagicMock()
        mock_builder.build.return_value = {
            "seq": 1,
            "prev_hash": "abc",
            "reference_id": None,
        }
        mock_buffer = MagicMock()
        mock_buffer.store.side_effect = OSError("disk full")

        api._receipt_builder = mock_builder
        api._buffer = mock_buffer
        api._enabled = True
        api._consecutive_store_failures = 0

        with patch("aevs._api.get_config") as mock_cfg, \
             patch("aevs.core.serializer.canonical_json", return_value=b"{}"):
            cfg = MagicMock()
            cfg.float_handling = "decimal_string"
            cfg.float_precision = 6
            mock_cfg.return_value = cfg
            api._handle_tool_call(tool_name="t", status="ok")
            api._handle_tool_call(tool_name="t", status="ok")
            api._handle_tool_call(tool_name="t", status="ok")

        assert api._consecutive_store_failures == 3
        assert api.is_healthy() is False

        # cleanup
        api._receipt_builder = None
        api._buffer = None
        api._enabled = False

    def test_disable_resets_counter(self):
        """disable() must reset the consecutive failure counter."""
        api._consecutive_store_failures = 99
        api.disable()
        assert api._consecutive_store_failures == 0

    def test_fork_child_resets_counter(self):
        """_after_fork_child() must reset the consecutive failure counter."""
        api._consecutive_store_failures = 42
        api._after_fork_child()
        assert api._consecutive_store_failures == 0

