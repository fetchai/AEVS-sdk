import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from aevs.core.serializer import canonical_json, truncate_field

_VECTORS_PATH = (
    Path(__file__).resolve().parents[2] / "tests" / "shared" / "canonical_golden_vectors.json"
)


class TestCanonicalJson:
    def test_sorted_keys(self):
        result = canonical_json({"b": 1, "a": 2})
        assert result == b'{"a":2,"b":1}'

    def test_no_whitespace(self):
        result = canonical_json({"key": "value"})
        assert b" " not in result
        assert b"\n" not in result

    def test_deterministic(self):
        obj = {"z": [1, 2], "a": {"nested": True}}
        assert canonical_json(obj) == canonical_json(obj)

    def test_none(self):
        assert canonical_json({"k": None}) == b'{"k":null}'

    def test_bool(self):
        result = json.loads(canonical_json({"t": True, "f": False}))
        assert result == {"f": False, "t": True}

    def test_int(self):
        assert canonical_json({"n": 42}) == b'{"n":42}'

    def test_string(self):
        assert canonical_json({"s": "hello"}) == b'{"s":"hello"}'

    def test_nested_dict(self):
        obj = {"outer": {"inner": 1}}
        parsed = json.loads(canonical_json(obj))
        assert parsed == {"outer": {"inner": 1}}

    def test_list(self):
        assert canonical_json({"l": [3, 1, 2]}) == b'{"l":[3,1,2]}'

    def test_tuple_becomes_list(self):
        assert canonical_json({"t": (1, 2)}) == b'{"t":[1,2]}'


class TestFloatHandling:
    def test_decimal_string_default(self):
        result = json.loads(canonical_json({"v": 3.14}))
        assert result["v"] == "3.140000"

    def test_custom_precision(self):
        result = json.loads(canonical_json({"v": 3.14}, float_precision=2))
        assert result["v"] == "3.14"

    def test_raise_mode_falls_through_to_decimal(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            result = json.loads(canonical_json({"v": 1.5}, float_handling="raise"))
        assert result["v"] == "1.500000"
        assert "strict mode" in caplog.text

    def test_float_in_nested_list(self):
        result = json.loads(canonical_json({"l": [1, 2.5, 3]}))
        assert result["l"] == [1, "2.500000", 3]

    def test_float_in_nested_dict(self):
        result = json.loads(canonical_json({"d": {"score": 0.99}}))
        assert result["d"]["score"] == "0.990000"

    def test_nan_becomes_null(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            result = json.loads(canonical_json({"v": float("nan")}))
        assert result["v"] is None
        assert "not valid JSON" in caplog.text

    def test_inf_becomes_null(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            result = json.loads(canonical_json({"v": float("inf")}))
        assert result["v"] is None
        assert "not valid JSON" in caplog.text

    def test_negative_inf_becomes_null(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            result = json.loads(canonical_json({"v": float("-inf")}))
        assert result["v"] is None
        assert "not valid JSON" in caplog.text

    def test_nan_in_nested_structure_becomes_null(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            result = json.loads(canonical_json({"outer": {"inner": [1, float("nan")]}}))
        assert result["outer"]["inner"] == [1, None]
        assert "not valid JSON" in caplog.text


class TestSpecialTypes:
    def test_bytes_base64(self):
        result = json.loads(canonical_json({"b": b"\x00\x01\x02"}))
        assert result["b"] == "AAEC"

    def test_datetime(self):
        dt = datetime(2026, 3, 30, 10, 0, 0, tzinfo=timezone.utc)
        result = json.loads(canonical_json({"dt": dt}))
        assert result["dt"] == "2026-03-30T10:00:00+00:00"

    def test_date(self):
        d = date(2026, 3, 30)
        result = json.loads(canonical_json({"d": d}))
        assert result["d"] == "2026-03-30"

    def test_unknown_type_becomes_string(self):
        class Custom:
            def __str__(self):
                return "custom_value"

        result = json.loads(canonical_json({"c": Custom()}))
        assert result["c"] == "custom_value"

    def test_non_string_dict_keys(self):
        result = json.loads(canonical_json({1: "one", 2: "two"}))  # type: ignore[dict-item]
        assert result == {"1": "one", "2": "two"}


class TestGoldenVectors:
    """Hardcoded input → expected-bytes pairs that match the backend's
    canonical_json output (BE/aevs_backend/canonical.py).

    If any of these fail after a serializer change, the SDK and backend
    will produce different bytes for the same logical data — breaking
    payload_hmac verification.
    """

    def test_sorted_keys_and_minimal_whitespace(self):
        assert canonical_json({"b": 1, "a": 2}) == b'{"a":2,"b":1}'

    def test_nested_sorted_keys(self):
        obj = {"z": {"b": 2, "a": 1}, "a": 0}
        assert canonical_json(obj) == b'{"a":0,"z":{"a":1,"b":2}}'

    def test_null_bool_int(self):
        obj = {"b": True, "f": False, "i": 42, "n": None}
        assert canonical_json(obj) == b'{"b":true,"f":false,"i":42,"n":null}'

    def test_string_nfc_normalization(self):
        # e + combining acute accent (NFD) -> precomposed e-acute (NFC)
        nfd = "e\u0301"  # decomposed
        nfc = "\u00e9"   # precomposed
        assert canonical_json({"v": nfd}) == canonical_json({"v": nfc})
        assert canonical_json({"v": nfd}) == b'{"v":"\xc3\xa9"}'

    def test_string_already_nfc(self):
        assert canonical_json({"s": "hello"}) == b'{"s":"hello"}'

    def test_float_decimal_string(self):
        assert canonical_json({"v": 3.14}) == b'{"v":"3.140000"}'

    def test_list_preserves_order(self):
        assert canonical_json({"l": [3, 1, 2]}) == b'{"l":[3,1,2]}'

    def test_unicode_passthrough(self):
        assert canonical_json({"e": "\U0001f525"}) == b'{"e":"\xf0\x9f\x94\xa5"}'

    def test_empty_structures(self):
        assert canonical_json({"d": {}, "l": []}) == b'{"d":{},"l":[]}'

    def test_nested_nfc_in_list(self):
        nfd = "n\u0303"  # n + combining tilde (NFD)
        nfc = "\u00f1"   # precomposed n-tilde (NFC)
        assert canonical_json({"l": [nfd]}) == canonical_json({"l": [nfc]})


@pytest.mark.skipif(
    not _VECTORS_PATH.exists(),
    reason=f"Golden vectors file not found: {_VECTORS_PATH}",
)
class TestSharedGoldenVectors:
    """Cross-repo golden vectors loaded from tests/shared/canonical_golden_vectors.json.

    The same file is tested by BE/tests/test_canonical.py. A failure here
    means the SDK's canonical_json has drifted from the byte-level contract
    and payload_hmac verification will break on the backend.
    """

    @pytest.fixture(scope="class")
    def vectors(self):
        with open(_VECTORS_PATH) as f:
            return json.load(f)["vectors"]

    def test_vectors_file_exists(self):
        assert _VECTORS_PATH.exists(), f"Golden vectors file missing: {_VECTORS_PATH}"

    def test_all_vectors_match(self, vectors):
        for v in vectors:
            expected = bytes.fromhex(v["expected_hex"])
            actual = canonical_json(v["input"], float_handling="raise")
            assert actual == expected, (
                f"Vector {v['name']!r} mismatch:\n"
                f"  expected: {expected!r}\n"
                f"  actual:   {actual!r}"
            )


class TestTruncateField:
    def test_no_truncation(self):
        data, truncated = truncate_field("short", max_bytes=1000)
        assert data == "short"
        assert truncated is False

    def test_truncation(self):
        large = "x" * 10_000
        data, truncated = truncate_field(large, max_bytes=100)
        assert truncated is True
        assert data["_truncated"] is True
        assert "_original_bytes" in data
        assert "_preview" in data

    def test_serialization_error(self):
        class BrokenStr:
            def __str__(self):
                raise RuntimeError("cannot convert")

        data, truncated = truncate_field(BrokenStr(), max_bytes=100)
        assert truncated is True
        assert data["_reason"] == "serialization_error"
