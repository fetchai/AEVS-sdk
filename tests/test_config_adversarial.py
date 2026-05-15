"""Adversarial tests for config.py — designed to break validation.

Targets uncovered lines: _parse_api_key hex ValueError path,
validation for drain_interval_ms, max_reference_entries,
max_payload_bytes, and repr with short api key.
"""

from __future__ import annotations

import pytest

from aevs.config import AEVSConfig, _parse_api_key, configure, get_config
from aevs.exceptions import AEVSConfigError
from tests.conftest import TEST_AGENT_ID, TEST_API_KEY


class TestParseApiKeyHexValidation:
    def test_odd_length_hex_triggers_fromhex_valueerror(self):
        """Odd-length hex that passes the minimum length check but fails
        bytes.fromhex — must raise AEVSConfigError."""
        odd_hex = "a" * 33  # 33 hex chars → passes ≥32 check but odd-length
        with pytest.raises(AEVSConfigError, match="not valid hex"):
            _parse_api_key(f"aevs_sk_testkey_{odd_hex}")

    def test_short_secret_rejected(self):
        """Secrets shorter than 16 bytes (32 hex chars) must be rejected."""
        with pytest.raises(AEVSConfigError, match="too short"):
            _parse_api_key("aevs_sk_mykey_ab")

    def test_exactly_min_length_accepted(self):
        key_id, secret = _parse_api_key("aevs_sk_mykey_" + "ab" * 16)
        assert key_id == "mykey"
        assert secret == b"\xab" * 16


class TestConfigValidationEdgeCases:
    def test_zero_drain_interval_autocorrects(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, drain_interval_ms=0)
        assert "drain_interval_ms" in caplog.text
        assert get_config().drain_interval_ms == 5_000

    def test_negative_drain_interval_autocorrects(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, drain_interval_ms=-100)
        assert "drain_interval_ms" in caplog.text
        assert get_config().drain_interval_ms == 5_000

    def test_zero_max_reference_entries_autocorrects(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, max_reference_entries=0)
        assert "max_reference_entries" in caplog.text
        assert get_config().max_reference_entries == 1_000

    def test_negative_max_reference_entries_autocorrects(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, max_reference_entries=-1)
        assert "max_reference_entries" in caplog.text
        assert get_config().max_reference_entries == 1_000

    def test_zero_max_payload_bytes_autocorrects(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, max_payload_bytes=0)
        assert "max_payload_bytes" in caplog.text
        assert get_config().max_payload_bytes == 1_048_576

    def test_negative_max_payload_bytes_autocorrects(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, max_payload_bytes=-1)
        assert "max_payload_bytes" in caplog.text
        assert get_config().max_payload_bytes == 1_048_576

    def test_accepts_valid_raise_float_handling(self):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, float_handling="raise")
        assert get_config().float_handling == "raise"


class TestAEVSConfigRepr:
    def test_repr_with_short_api_key(self):
        """API keys <=16 chars show as '***' in repr."""
        short_key = "aevs_sk_k_" + "ab" * 3
        config = AEVSConfig(
            api_key=short_key,
            key_id="k",
            key_secret=b"\xab" * 16,
            agent_id=TEST_AGENT_ID,
        )
        r = repr(config)
        assert "***" in r
        assert short_key not in r

    def test_repr_with_long_api_key(self):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID)
        cfg = get_config()
        r = repr(cfg)
        assert "..." in r
        assert TEST_API_KEY not in r
