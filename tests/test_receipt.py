from datetime import datetime, timezone

from aevs._version import __version__
from aevs.config import configure, get_config
from aevs.core.receipt import ReceiptBuilder
from aevs.core.serializer import canonical_json
from aevs.crypto.chain import compute_chain_anchor
from aevs.crypto.hkdf import derive_key
from aevs.crypto.hmac_auth import verify_hmac
from tests.conftest import TEST_API_KEY

# Fixed session_id makes anchor-equality assertions reproducible across
# tests; production code mints a fresh UUID per enable().
_TEST_SESSION_ID = "00000000-0000-4000-8000-000000000001"


def _make_builder(*, session_id: str = _TEST_SESSION_ID, **kwargs) -> ReceiptBuilder:
    configure(api_key=TEST_API_KEY, **kwargs)
    return ReceiptBuilder(get_config(), session_id=session_id)


def _build_one(builder: ReceiptBuilder, **overrides) -> dict:
    defaults = {
        "tool_name": "search_web",
        "inputs": {"query": "weather NYC"},
        "output": {"result": "Sunny, 72°F"},
        "status": "success",
        "error": None,
        "started_at": datetime(2026, 3, 30, 10, 0, 0, tzinfo=timezone.utc),
        "ended_at": datetime(2026, 3, 30, 10, 0, 0, 450000, tzinfo=timezone.utc),
        "framework": "langchain",
        "framework_version": "0.3.1",
    }
    defaults.update(overrides)
    return builder.build(**defaults)


class TestReceiptBuilder:
    def test_builds_receipt(self):
        builder = _make_builder()
        receipt = _build_one(builder)

        assert receipt["tool_name"] == "search_web"
        assert receipt["inputs"] == {"query": "weather NYC"}
        assert receipt["status"] == "success"
        assert receipt["seq"] == 1
        assert receipt["duration_ms"] == 450
        assert receipt["sdk_version"] == __version__
        assert receipt["framework"] == "langchain"
        assert "payload_hmac" in receipt
        assert "prev_hash" in receipt
        assert "reference_id" in receipt
        assert len(receipt["reference_id"]) == 36
        assert receipt["session_id"] == _TEST_SESSION_ID

    def test_session_id_constant_within_builder(self):
        """Every receipt produced by one builder shares the builder's
        session_id — that's what makes the chain anchored to a single
        session."""
        builder = _make_builder()
        receipts = [_build_one(builder) for _ in range(3)]
        assert {r["session_id"] for r in receipts} == {_TEST_SESSION_ID}

    def test_session_id_differs_across_builders(self):
        """Two independently constructed builders carry different
        session_ids by default — caller is responsible for picking."""
        b1 = _make_builder(session_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        b2 = _make_builder(session_id="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
        r1 = _build_one(b1)
        r2 = _build_one(b2)
        assert r1["session_id"] != r2["session_id"]
        assert r1["prev_hash"] != r2["prev_hash"], (
            "different session_id must yield a different anchor "
            "(else the cryptographic isolation is broken)"
        )

    def test_session_id_covered_by_payload_hmac(self):
        """Tampering with session_id post-build must invalidate the HMAC
        so a forged session boundary is detectable."""
        builder = _make_builder()
        receipt = _build_one(builder)
        cfg = get_config()
        payload_key = derive_key(cfg.key_secret, salt="aevs-payload-v1")
        original_hmac = receipt.pop("payload_hmac")

        # Sanity: the unmodified canonical bytes verify.
        assert verify_hmac(
            payload_key,
            canonical_json(receipt, float_handling=cfg.float_handling,
                           float_precision=cfg.float_precision),
            original_hmac,
        )

        # Flip the session_id and the HMAC must no longer verify.
        receipt["session_id"] = "ffffffff-ffff-4fff-8fff-ffffffffffff"
        assert not verify_hmac(
            payload_key,
            canonical_json(receipt, float_handling=cfg.float_handling,
                           float_precision=cfg.float_precision),
            original_hmac,
        )

    def test_sequence_increments(self):
        builder = _make_builder()
        r1 = _build_one(builder)
        r2 = _build_one(builder)
        r3 = _build_one(builder)
        assert r1["seq"] == 1
        assert r2["seq"] == 2
        assert r3["seq"] == 3

    def test_first_prev_hash_is_chain_anchor(self):
        builder = _make_builder()
        cfg = get_config()
        receipt = _build_one(builder)
        expected_anchor = compute_chain_anchor(cfg.key_secret, _TEST_SESSION_ID)
        assert receipt["prev_hash"] == expected_anchor

    def test_chain_links(self):
        builder = _make_builder()
        r1 = _build_one(builder)
        r2 = _build_one(builder)
        # r2's prev_hash should be the hash of r1's complete canonical JSON
        assert r2["prev_hash"] != r1["prev_hash"]
        assert len(r2["prev_hash"]) == 64

    def test_payload_hmac_verifiable(self):
        builder = _make_builder()
        receipt = _build_one(builder)
        cfg = get_config()

        payload_key = derive_key(cfg.key_secret, salt="aevs-payload-v1")
        hmac_value = receipt.pop("payload_hmac")

        receipt_bytes = canonical_json(
            receipt,
            float_handling=cfg.float_handling,
            float_precision=cfg.float_precision,
        )
        assert verify_hmac(payload_key, receipt_bytes, hmac_value)

    def test_error_receipt(self):
        builder = _make_builder()
        receipt = _build_one(builder, status="error", error="ConnectionTimeout", output=None)
        assert receipt["status"] == "error"
        assert receipt["error"] == "ConnectionTimeout"
        assert receipt["output"] is None

    def test_agent_id(self):
        builder = _make_builder(agent_id="agt_test")
        receipt = _build_one(builder)
        assert receipt["agent_id"] == "agt_test"

    def test_agent_id_none_by_default(self):
        builder = _make_builder()
        receipt = _build_one(builder)
        assert receipt["agent_id"] is None

    def test_run_ids(self):
        builder = _make_builder()
        receipt = _build_one(builder, run_id="run-123", parent_run_id="run-000")
        assert receipt["run_id"] == "run-123"
        assert receipt["parent_run_id"] == "run-000"

    def test_truncates_large_inputs(self):
        builder = _make_builder()
        large_input = {"data": "x" * 2_000_000}
        receipt = _build_one(builder, inputs=large_input)
        assert receipt["inputs"]["_truncated"] is True

    def test_deterministic_fields(self):
        """Deterministic fields match across independent builders; reference_id differs."""
        b1 = _make_builder()
        b2 = _make_builder()
        r1 = _build_one(b1)
        r2 = _build_one(b2)
        assert r1["prev_hash"] == r2["prev_hash"]
        assert r1["seq"] == r2["seq"]
        assert r1["tool_name"] == r2["tool_name"]
        assert r1["reference_id"] != r2["reference_id"]
