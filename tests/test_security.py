"""Security & adversarial tests — white-hat hacker perspective.

Tests verify crypto integrity, resilience to malicious inputs,
and the 'never break the agent' contract under adversarial conditions.
"""

from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest
import respx
from langchain_core.tools import tool

import aevs
import aevs._api as _api_mod
from aevs.config import _parse_api_key, configure, get_config, reset_config
from aevs.core.buffer import LocalBuffer
from aevs.core.receipt import ReceiptBuilder
from aevs.core.serializer import canonical_json, truncate_field
from aevs.core.signer import sign_request
from aevs.crypto.chain import compute_chain_anchor, compute_receipt_hash
from aevs.crypto.hkdf import derive_key
from aevs.crypto.hmac_auth import compute_hmac, verify_hmac
from aevs.exceptions import AEVSConfigError
from tests.conftest import (
    TEST_API_KEY,
    TEST_BASE_URL,
    TEST_KEY_SECRET,
    TEST_RECEIPTS_URL,
)


def _store_dict(buf: LocalBuffer, d: dict, *, prev_hash: str = "h") -> None:
    """Helper: serialize a receipt dict and store via the bytes-based API."""
    buf.store(d["seq"], canonical_json(d), prev_hash=prev_hash)

FIXED_START = datetime(2026, 3, 30, 10, 0, 0, tzinfo=timezone.utc)
FIXED_END = datetime(2026, 3, 30, 10, 0, 0, 500000, tzinfo=timezone.utc)


def _make_builder(**kwargs) -> ReceiptBuilder:
    configure(api_key=TEST_API_KEY, **kwargs)
    return ReceiptBuilder(get_config())


def _build_receipt(builder: ReceiptBuilder, **overrides) -> dict:
    defaults = {
        "tool_name": "search",
        "inputs": {"q": "test"},
        "output": {"r": "result"},
        "status": "success",
        "error": None,
        "started_at": FIXED_START,
        "ended_at": FIXED_END,
        "framework": "langchain",
        "framework_version": "0.3.1",
    }
    defaults.update(overrides)
    return builder.build(**defaults)


# ===================================================================
# 1. HMAC TAMPER DETECTION
# ===================================================================


class TestHmacTampering:
    """Any modification to a receipt must invalidate its HMAC."""

    def _verify_tampering_detected(self, receipt: dict, mutate_fn):
        cfg = get_config()
        payload_key = derive_key(cfg.key_secret, salt="aevs-payload-v1")
        original_hmac = receipt.pop("payload_hmac")

        mutate_fn(receipt)

        receipt_bytes = canonical_json(
            receipt,
            float_handling=cfg.float_handling,
            float_precision=cfg.float_precision,
        )
        assert not verify_hmac(payload_key, receipt_bytes, original_hmac)

    def test_tamper_tool_name(self):
        builder = _make_builder()
        r = _build_receipt(builder)
        self._verify_tampering_detected(r, lambda d: d.__setitem__("tool_name", "evil"))

    def test_tamper_seq(self):
        builder = _make_builder()
        r = _build_receipt(builder)
        self._verify_tampering_detected(r, lambda d: d.__setitem__("seq", 999))

    def test_tamper_output(self):
        builder = _make_builder()
        r = _build_receipt(builder)
        self._verify_tampering_detected(r, lambda d: d.__setitem__("output", "FAKE"))

    def test_tamper_status(self):
        builder = _make_builder()
        r = _build_receipt(builder)
        self._verify_tampering_detected(
            r, lambda d: d.__setitem__("status", "error")
        )

    def test_inject_extra_field(self):
        builder = _make_builder()
        r = _build_receipt(builder)
        self._verify_tampering_detected(
            r, lambda d: d.__setitem__("injected", "hacker")
        )

    def test_remove_field(self):
        builder = _make_builder()
        r = _build_receipt(builder)
        self._verify_tampering_detected(r, lambda d: d.pop("error"))

    def test_wrong_key_cannot_forge_hmac(self):
        builder = _make_builder()
        receipt = _build_receipt(builder)
        cfg = get_config()

        attacker_key = derive_key(b"\x00" * 32, salt="aevs-payload-v1")
        real_hmac = receipt.pop("payload_hmac")
        receipt_bytes = canonical_json(
            receipt,
            float_handling=cfg.float_handling,
            float_precision=cfg.float_precision,
        )
        forged_hmac = compute_hmac(attacker_key, receipt_bytes)
        assert forged_hmac != real_hmac

    def test_partial_signature_match_rejected(self):
        key = b"secret" * 5
        msg = b"message"
        correct = compute_hmac(key, msg)
        assert not verify_hmac(key, msg, correct[:32] + "0" * 32)
        assert not verify_hmac(key, msg, "")
        assert not verify_hmac(key, msg, "0" * 64)


# ===================================================================
# 2. HASH CHAIN INTEGRITY
# ===================================================================


class TestHashChainIntegrity:
    """Verify the chain detects deletion, reordering, and forgery."""

    def test_chain_links_are_correct(self):
        builder = _make_builder()
        cfg = get_config()
        r1 = _build_receipt(builder)
        r2 = _build_receipt(builder)
        r3 = _build_receipt(builder)

        assert r1["prev_hash"] == compute_chain_anchor(cfg.key_secret)

        r1_bytes = canonical_json(
            r1, float_handling=cfg.float_handling, float_precision=cfg.float_precision
        )
        assert r2["prev_hash"] == compute_receipt_hash(r1_bytes)

        r2_bytes = canonical_json(
            r2, float_handling=cfg.float_handling, float_precision=cfg.float_precision
        )
        assert r3["prev_hash"] == compute_receipt_hash(r2_bytes)

    def test_deleting_middle_breaks_chain(self):
        builder = _make_builder()
        cfg = get_config()
        r1 = _build_receipt(builder)
        _build_receipt(builder)  # r2 — deleted
        r3 = _build_receipt(builder)

        r1_bytes = canonical_json(
            r1, float_handling=cfg.float_handling, float_precision=cfg.float_precision
        )
        # r3 links to r2, not r1 — backend would see a gap
        assert r3["prev_hash"] != compute_receipt_hash(r1_bytes)

    def test_reorder_detected(self):
        builder = _make_builder()
        _build_receipt(builder)  # r1 — advances the chain
        r2 = _build_receipt(builder)
        # If backend receives r2 first, its prev_hash won't match the anchor
        cfg = get_config()
        assert r2["prev_hash"] != compute_chain_anchor(cfg.key_secret)

    def test_cannot_forge_anchor_without_key(self):
        real = compute_chain_anchor(TEST_KEY_SECRET)
        fake = compute_chain_anchor(b"\xff" * 32)
        assert real != fake

    def test_chain_deterministic_across_builders(self):
        b1 = _make_builder()
        b2 = _make_builder()
        r1 = _build_receipt(b1)
        r2 = _build_receipt(b2)
        assert r1["prev_hash"] == r2["prev_hash"]
        assert r1["reference_id"] != r2["reference_id"]


# ===================================================================
# 3. KEY DERIVATION ISOLATION
# ===================================================================


class TestKeyIsolation:
    def test_all_purpose_keys_are_different(self):
        salts = ["aevs-request-v1", "aevs-payload-v1", "aevs-encrypt-v1", "aevs-chain-v1"]
        keys = [derive_key(TEST_KEY_SECRET, salt=s) for s in salts]
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                assert keys[i] != keys[j], f"salt[{i}] == salt[{j}]"

    def test_all_keys_are_256_bits(self):
        for salt in ("aevs-request-v1", "aevs-payload-v1", "aevs-encrypt-v1", "aevs-chain-v1"):
            assert len(derive_key(TEST_KEY_SECRET, salt=salt)) == 32

    def test_different_secrets_produce_different_keys(self):
        k1 = derive_key(b"\x01" * 32, salt="aevs-request-v1")
        k2 = derive_key(b"\x02" * 32, salt="aevs-request-v1")
        assert k1 != k2


# ===================================================================
# 4. BUFFER ENCRYPTION ATTACKS
# ===================================================================


class TestBufferEncryptionAttacks:

    def test_ciphertext_bit_flip_detected(self, tmp_path):
        """AES-GCM auth tag must detect bit flips in ciphertext."""
        buf = LocalBuffer(tmp_path / "flip.db", TEST_KEY_SECRET)
        _store_dict(buf, {"seq": 1, "tool_name": "t", "payload_hmac": "h"})

        conn = sqlite3.connect(str(tmp_path / "flip.db"))
        row = conn.execute("SELECT receipt_enc FROM receipts WHERE seq = 1").fetchone()
        tampered = bytearray(row[0])
        tampered[15] ^= 0xFF  # flip a ciphertext byte
        conn.execute("UPDATE receipts SET receipt_enc = ? WHERE seq = 1", (bytes(tampered),))
        conn.commit()
        conn.close()

        with pytest.raises(Exception):
            buf.get_pending()
        buf.close()

    def test_truncated_ciphertext_fails(self, tmp_path):
        buf = LocalBuffer(tmp_path / "trunc.db", TEST_KEY_SECRET)
        _store_dict(buf, {"seq": 1, "tool_name": "t", "payload_hmac": "h"})

        conn = sqlite3.connect(str(tmp_path / "trunc.db"))
        row = conn.execute("SELECT receipt_enc FROM receipts WHERE seq = 1").fetchone()
        conn.execute("UPDATE receipts SET receipt_enc = ? WHERE seq = 1", (row[0][:12],))
        conn.commit()
        conn.close()

        with pytest.raises(Exception):
            buf.get_pending()
        buf.close()

    def test_empty_blob_fails(self, tmp_path):
        buf = LocalBuffer(tmp_path / "empty.db", TEST_KEY_SECRET)
        _store_dict(buf, {"seq": 1, "tool_name": "t", "payload_hmac": "h"})

        conn = sqlite3.connect(str(tmp_path / "empty.db"))
        conn.execute("UPDATE receipts SET receipt_enc = X'' WHERE seq = 1")
        conn.commit()
        conn.close()

        with pytest.raises(Exception):
            buf.get_pending()
        buf.close()

    def test_swapped_nonces_fail(self, tmp_path):
        """Using receipt A's nonce with receipt B's ciphertext must fail."""
        buf = LocalBuffer(tmp_path / "swap.db", TEST_KEY_SECRET)
        _store_dict(buf, {"seq": 1, "tool_name": "a", "payload_hmac": "x"})
        _store_dict(buf, {"seq": 2, "tool_name": "b", "payload_hmac": "y"})

        conn = sqlite3.connect(str(tmp_path / "swap.db"))
        rows = conn.execute(
            "SELECT seq, receipt_enc FROM receipts ORDER BY seq"
        ).fetchall()
        nonce_a, ct_b = rows[0][1][:12], rows[1][1][12:]
        conn.execute(
            "UPDATE receipts SET receipt_enc = ? WHERE seq = 2", (nonce_a + ct_b,)
        )
        conn.commit()
        conn.close()

        with pytest.raises(Exception):
            buf.get_pending()
        buf.close()

    def test_different_key_cannot_decrypt(self, tmp_path):
        buf1 = LocalBuffer(tmp_path / "k.db", b"\xaa" * 32)
        _store_dict(buf1, {"seq": 1, "tool_name": "t", "payload_hmac": "h"})
        buf1.close()

        buf2 = LocalBuffer(tmp_path / "k.db", b"\xbb" * 32)
        with pytest.raises(Exception):
            buf2.get_pending()
        buf2.close()


# ===================================================================
# 5. SERIALIZATION ATTACKS
# ===================================================================


class TestSerializationAttacks:

    def test_deep_nesting(self):
        obj: Any = {"v": "leaf"}
        for _ in range(200):
            obj = {"n": obj}
        result = canonical_json(obj)
        assert len(result) > 0

    def test_extreme_nesting_caught_by_truncate(self):
        obj: Any = {"v": "leaf"}
        for _ in range(sys.getrecursionlimit() + 50):
            obj = {"n": obj}
        data, truncated = truncate_field(obj, max_bytes=100)
        assert truncated
        assert data["_reason"] == "serialization_error"

    def test_circular_reference_caught(self):
        d: dict[str, Any] = {"key": "value"}
        d["self"] = d
        data, truncated = truncate_field(d, max_bytes=100)
        assert truncated
        assert data["_reason"] == "serialization_error"

    def test_null_bytes_in_strings(self):
        result = canonical_json({"t": "hello\x00world"})
        parsed = json.loads(result)
        assert "hello" in parsed["t"]

    def test_huge_integer(self):
        result = canonical_json({"n": 10**1000})
        parsed = json.loads(result)
        assert parsed["n"] == 10**1000

    def test_emoji_strings(self):
        result = canonical_json({"e": "🔥" * 500})
        parsed = json.loads(result)
        assert len(parsed["e"]) == 500

    def test_evil_repr_in_truncate_field(self):
        class Evil:
            def __repr__(self):
                raise RuntimeError("boom")
            def __str__(self):
                raise RuntimeError("boom")

        data, truncated = truncate_field(Evil(), max_bytes=100)
        assert truncated
        assert data["_reason"] == "serialization_error"

    def test_object_returning_huge_string(self):
        class HugeStr:
            def __str__(self):
                return "x" * 50_000_000

        data, truncated = truncate_field(HugeStr(), max_bytes=1000)
        assert truncated
        assert "_original_bytes" in data


# ===================================================================
# 6. SQL INJECTION
# ===================================================================


class TestSqlInjection:
    def test_injection_in_tool_name(self, tmp_path):
        buf = LocalBuffer(tmp_path / "sqli.db", TEST_KEY_SECRET)
        evil = {"seq": 1, "tool_name": "'; DROP TABLE receipts; --", "payload_hmac": "x"}
        _store_dict(buf, evil)
        pending = buf.get_pending()
        assert len(pending) == 1
        _, payload = pending[0]
        assert json.loads(payload)["tool_name"] == "'; DROP TABLE receipts; --"
        buf.close()

    def test_injection_in_prev_hash(self, tmp_path):
        buf = LocalBuffer(tmp_path / "sqli2.db", TEST_KEY_SECRET)
        _store_dict(
            buf, {"seq": 1, "tool_name": "t", "payload_hmac": "x"},
            prev_hash="'; DROP TABLE receipts; --",
        )
        assert buf.pending_count() == 1
        buf.close()

    def test_injection_in_status_via_mark_flushed(self, tmp_path):
        """mark_flushed uses IN (?) — verify no injection through seq list."""
        buf = LocalBuffer(tmp_path / "sqli3.db", TEST_KEY_SECRET)
        _store_dict(buf, {"seq": 1, "tool_name": "t", "payload_hmac": "x"})
        buf.mark_flushed([1])
        assert buf.pending_count() == 0
        buf.close()


# ===================================================================
# 7. CONFIG & KEY SECURITY
# ===================================================================


class TestConfigSecurity:
    def test_api_key_not_in_repr(self):
        configure(api_key=TEST_API_KEY)
        cfg = get_config()
        assert TEST_API_KEY not in repr(cfg)

    def test_key_secret_hex_not_in_repr(self):
        configure(api_key=TEST_API_KEY)
        cfg = get_config()
        assert TEST_KEY_SECRET.hex() not in repr(cfg)

    def test_api_key_not_in_str(self):
        configure(api_key=TEST_API_KEY)
        cfg = get_config()
        assert TEST_API_KEY not in str(cfg)

    def test_config_immutable(self):
        configure(api_key=TEST_API_KEY)
        cfg = get_config()
        with pytest.raises(AttributeError):
            cfg.key_secret = b"hacked"  # type: ignore[misc]
        with pytest.raises(AttributeError):
            cfg.api_key = "hacked"  # type: ignore[misc]
        with pytest.raises(AttributeError):
            cfg.base_url = "http://evil.com"  # type: ignore[misc]

    def test_api_key_regex_rejects_malformed(self):
        evil_keys = [
            "aevs_sk_../../../etc/passwd_abcd1234",
            "aevs_sk_test key_abcd1234",
            "aevs_sk_test\x00key_abcd1234",
            "",
            "a" * 10000,
            "aevs_sk__abcd1234",
        ]
        for key in evil_keys:
            with pytest.raises(AEVSConfigError):
                _parse_api_key(key)


# ===================================================================
# 8. SIGNATURE REPLAY & FORGERY
# ===================================================================


class TestSignatureSecurity:
    def test_different_payloads_different_signatures(self):
        configure(api_key=TEST_API_KEY)
        cfg = get_config()
        ts = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc)
        s1 = sign_request(cfg, b'{"seq":1}', timestamp=ts)
        s2 = sign_request(cfg, b'{"seq":2}', timestamp=ts)
        assert s1["X-AEVS-Signature"] != s2["X-AEVS-Signature"]

    def test_same_payload_different_timestamp_different_sig(self):
        configure(api_key=TEST_API_KEY)
        cfg = get_config()
        ts1 = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc)
        ts2 = datetime(2026, 3, 30, 12, 0, 1, tzinfo=timezone.utc)
        s1 = sign_request(cfg, b'{"seq":1}', timestamp=ts1)
        s2 = sign_request(cfg, b'{"seq":1}', timestamp=ts2)
        assert s1["X-AEVS-Signature"] != s2["X-AEVS-Signature"]

    def test_replayed_sig_with_altered_timestamp_fails(self):
        import hashlib

        configure(api_key=TEST_API_KEY)
        cfg = get_config()
        payload = b'{"seq":1}'
        ts = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc)

        headers = sign_request(cfg, payload, timestamp=ts)
        original_sig = headers["X-AEVS-Signature"]

        fake_ts = datetime(2026, 3, 30, 13, 0, 0, tzinfo=timezone.utc)
        signing_key = derive_key(cfg.key_secret, salt="aevs-request-v1")
        ph = hashlib.sha256(payload).hexdigest()
        fake_string_to_sign = f"{fake_ts.isoformat()}\n{ph}"

        assert not verify_hmac(
            signing_key, fake_string_to_sign.encode("utf-8"), original_sig
        )

    def test_wrong_key_cannot_forge_signature(self):
        configure(api_key=TEST_API_KEY)
        cfg = get_config()
        ts = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc)

        real_headers = sign_request(cfg, b"payload", timestamp=ts)

        attacker_key = derive_key(b"\x00" * 32, salt="aevs-request-v1")
        import hashlib

        ph = hashlib.sha256(b"payload").hexdigest()
        sts = f"{ts.isoformat()}\n{ph}"
        forged_sig = compute_hmac(attacker_key, sts.encode("utf-8"))

        assert forged_sig != real_headers["X-AEVS-Signature"]


# ===================================================================
# 9. AGENT CRASH RESILIENCE (full integration)
# ===================================================================


@tool
def adder(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@pytest.fixture(autouse=True)
def _sec_cleanup(tmp_path):
    reset_config()
    yield
    aevs.disable()
    reset_config()


def _sec_configure(tmp_path, **kwargs):
    kwargs.setdefault("base_url", TEST_BASE_URL)
    aevs.configure(
        api_key=TEST_API_KEY,
        buffer_path=str(tmp_path / "sec_buffer.db"),
        **kwargs,
    )


class TestAgentCrashResilience:
    """The SDK must NEVER crash the user's agent."""

    @respx.mock
    def test_nan_output_does_not_crash(self, tmp_path):
        route = respx.post(TEST_RECEIPTS_URL).mock(
            return_value=httpx.Response(200)
        )
        _sec_configure(tmp_path)
        aevs.enable(frameworks=["langchain"])

        @tool
        def nan_tool(x: int) -> float:
            """Returns nan."""
            return float("nan")

        result = nan_tool.invoke({"x": 1})
        assert result != result  # nan != nan
        aevs.flush()
        assert route.called

    @respx.mock
    def test_huge_output_does_not_crash(self, tmp_path):
        respx.post(TEST_RECEIPTS_URL).mock(
            return_value=httpx.Response(200)
        )
        _sec_configure(tmp_path)
        aevs.enable(frameworks=["langchain"])

        @tool
        def big_tool(x: int) -> str:
            """Huge output."""
            return "A" * 5_000_000

        result = big_tool.invoke({"x": 1})
        assert len(result) == 5_000_000

    @respx.mock
    def test_backend_garbage_response(self, tmp_path):
        respx.post(TEST_RECEIPTS_URL).mock(
            return_value=httpx.Response(200, content=b"NOT JSON {{{")
        )
        _sec_configure(tmp_path)
        aevs.enable(frameworks=["langchain"])
        assert adder.invoke({"a": 1, "b": 2}) == 3

    @respx.mock
    def test_backend_timeout(self, tmp_path):
        respx.post(TEST_RECEIPTS_URL).mock(
            side_effect=httpx.ReadTimeout("timeout")
        )
        _sec_configure(tmp_path)
        aevs.enable(frameworks=["langchain"])
        assert adder.invoke({"a": 10, "b": 20}) == 30

    @respx.mock
    def test_backend_connection_reset(self, tmp_path):
        respx.post(TEST_RECEIPTS_URL).mock(
            side_effect=httpx.RemoteProtocolError("connection reset")
        )
        _sec_configure(tmp_path)
        aevs.enable(frameworks=["langchain"])
        assert adder.invoke({"a": 3, "b": 4}) == 7

    @respx.mock
    def test_corrupted_builder_does_not_crash(self, tmp_path):
        """Even if SDK internals are corrupted, the agent must survive."""
        respx.post(TEST_RECEIPTS_URL).mock(
            return_value=httpx.Response(200)
        )
        _sec_configure(tmp_path)
        aevs.enable(frameworks=["langchain"])

        _api_mod._receipt_builder._config = None
        assert adder.invoke({"a": 7, "b": 3}) == 10

    @respx.mock
    def test_inf_output_does_not_crash(self, tmp_path):
        respx.post(TEST_RECEIPTS_URL).mock(
            return_value=httpx.Response(200)
        )
        _sec_configure(tmp_path)
        aevs.enable(frameworks=["langchain"])

        @tool
        def inf_tool(x: int) -> float:
            """Returns infinity."""
            return float("inf")

        result = inf_tool.invoke({"x": 1})
        assert result == float("inf")


# ===================================================================
# 10. THREAD SAFETY UNDER LOAD
# ===================================================================


class TestThreadSafety:
    @respx.mock
    def test_concurrent_tool_calls(self, tmp_path):
        """Many threads calling tools must not corrupt seq or crash."""
        bodies: list[dict] = []
        lock = threading.Lock()

        def capture(request: httpx.Request) -> httpx.Response:
            with lock:
                bodies.append(json.loads(request.content))
            return httpx.Response(200)

        respx.post(TEST_RECEIPTS_URL).mock(side_effect=capture)
        _sec_configure(tmp_path)
        aevs.enable(frameworks=["langchain"])

        n = 20

        def call(i: int):
            return adder.invoke({"a": i, "b": i})

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
            futures = [pool.submit(call, i) for i in range(n)]
            results = [f.result() for f in futures]

        assert results == [i * 2 for i in range(n)]

        aevs.flush()

        seqs = sorted(b["seq"] for b in bodies)
        assert seqs == list(range(1, n + 1))
        assert len(set(seqs)) == n  # no duplicates

    @respx.mock
    def test_disable_during_tool_calls(self, tmp_path):
        """disable() while tool calls are in flight must not crash."""
        respx.post(TEST_RECEIPTS_URL).mock(
            return_value=httpx.Response(200)
        )
        _sec_configure(tmp_path)
        aevs.enable(frameworks=["langchain"])

        barrier = threading.Barrier(2, timeout=5)
        results = []

        def tool_call():
            results.append(adder.invoke({"a": 1, "b": 1}))
            barrier.wait()

        def disable_mid_flight():
            barrier.wait()
            aevs.disable()

        t1 = threading.Thread(target=tool_call)
        t2 = threading.Thread(target=disable_mid_flight)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert results == [2]


# ===================================================================
# 11. BUFFER ABUSE
# ===================================================================


class TestBufferAbuse:
    def test_duplicate_seq_raises(self, tmp_path):
        """Inserting a receipt with a duplicate seq must fail (PK constraint)."""
        buf = LocalBuffer(tmp_path / "dup.db", TEST_KEY_SECRET)
        _store_dict(buf, {"seq": 1, "tool_name": "t", "payload_hmac": "x"})
        with pytest.raises(Exception):
            _store_dict(buf, {"seq": 1, "tool_name": "t2", "payload_hmac": "y"})
        buf.close()

    def test_store_after_close_raises(self, tmp_path):
        buf = LocalBuffer(tmp_path / "closed.db", TEST_KEY_SECRET)
        buf.close()
        with pytest.raises(Exception):
            _store_dict(buf, {"seq": 1, "tool_name": "t", "payload_hmac": "x"})

    def test_eviction_preserves_newest(self, tmp_path):
        buf = LocalBuffer(tmp_path / "evict.db", TEST_KEY_SECRET, max_records=5)
        for i in range(1, 8):
            _store_dict(buf, {"seq": i, "tool_name": f"t{i}", "payload_hmac": "x"})
        pending = buf.get_pending()
        seqs = [seq for seq, _ in pending]
        assert seqs == [3, 4, 5, 6, 7]
        buf.close()
