from pathlib import Path

import pytest

from aevs.config import _parse_api_key, configure, get_config, reset_config
from aevs.exceptions import AEVSConfigError
from tests.conftest import TEST_API_KEY, TEST_KEY_ID, TEST_KEY_SECRET


class TestParseApiKey:
    def test_valid_key(self):
        key_id, secret = _parse_api_key(TEST_API_KEY)
        assert key_id == TEST_KEY_ID
        assert secret == TEST_KEY_SECRET

    def test_rejects_empty(self):
        with pytest.raises(AEVSConfigError, match="Invalid API key format"):
            _parse_api_key("")

    def test_rejects_wrong_prefix(self):
        with pytest.raises(AEVSConfigError, match="Invalid API key format"):
            _parse_api_key("sk_testkey_abcd1234")

    def test_rejects_missing_secret(self):
        with pytest.raises(AEVSConfigError, match="Invalid API key format"):
            _parse_api_key("aevs_sk_testkey_")

    def test_rejects_non_hex_secret(self):
        with pytest.raises(AEVSConfigError, match="Invalid API key format"):
            _parse_api_key("aevs_sk_testkey_ZZZZ")


class TestConfigure:
    def test_basic(self):
        configure(api_key=TEST_API_KEY)
        cfg = get_config()
        assert cfg.key_id == TEST_KEY_ID
        assert cfg.key_secret == TEST_KEY_SECRET
        assert cfg.base_url == "https://aevs.fetch.ai/v1"
        assert cfg.float_handling == "decimal_string"

    def test_custom_values(self):
        configure(
            api_key=TEST_API_KEY,
            agent_id="agt_test",
            base_url="https://api.example.com/v1/",
            signing_timeout_ms=5000,
            float_precision=10,
        )
        cfg = get_config()
        assert cfg.agent_id == "agt_test"
        assert cfg.base_url == "https://api.example.com/v1"  # trailing slash stripped
        assert cfg.signing_timeout_ms == 5000
        assert cfg.float_precision == 10

    def test_last_wins(self):
        configure(api_key=TEST_API_KEY, agent_id="first")
        configure(api_key=TEST_API_KEY, agent_id="second")
        assert get_config().agent_id == "second"

    def test_rejects_bad_float_handling(self):
        with pytest.raises(AEVSConfigError, match="float_handling"):
            configure(api_key=TEST_API_KEY, float_handling="invalid")

    def test_rejects_negative_precision(self):
        with pytest.raises(AEVSConfigError, match="float_precision"):
            configure(api_key=TEST_API_KEY, float_precision=-1)

    def test_rejects_zero_timeout(self):
        with pytest.raises(AEVSConfigError, match="signing_timeout_ms"):
            configure(api_key=TEST_API_KEY, signing_timeout_ms=0)

    def test_buffer_path_accepts_string(self):
        configure(api_key=TEST_API_KEY, buffer_path="/tmp/test.db")
        assert get_config().buffer_path == Path("/tmp/test.db")

    def test_max_buffer_records_default(self):
        configure(api_key=TEST_API_KEY)
        assert get_config().max_buffer_records == 10_000

    def test_max_buffer_records_custom(self):
        configure(api_key=TEST_API_KEY, max_buffer_records=500)
        assert get_config().max_buffer_records == 500

    def test_rejects_zero_max_buffer_records(self):
        with pytest.raises(AEVSConfigError, match="max_buffer_records"):
            configure(api_key=TEST_API_KEY, max_buffer_records=0)

    def test_rejects_negative_max_buffer_records(self):
        with pytest.raises(AEVSConfigError, match="max_buffer_records"):
            configure(api_key=TEST_API_KEY, max_buffer_records=-1)

    def test_configure_while_enabled_raises(self, tmp_path):
        """configure() must reject calls while AEVS is enabled."""
        import aevs

        configure(api_key=TEST_API_KEY, buffer_path=str(tmp_path / "buf.db"))
        aevs.enable(frameworks=["langchain"])
        try:
            with pytest.raises(AEVSConfigError, match="Cannot reconfigure"):
                configure(api_key=TEST_API_KEY)
        finally:
            aevs.disable()

    def test_configure_after_disable_works(self, tmp_path):
        """configure() should work again after disable()."""
        import aevs

        configure(api_key=TEST_API_KEY, buffer_path=str(tmp_path / "buf.db"))
        aevs.enable(frameworks=["langchain"])
        aevs.disable()
        configure(api_key=TEST_API_KEY, agent_id="reconfigured")
        assert get_config().agent_id == "reconfigured"


class TestGetConfig:
    def test_raises_before_configure(self):
        with pytest.raises(AEVSConfigError, match="not configured"):
            get_config()

    def test_reset_clears(self):
        configure(api_key=TEST_API_KEY)
        reset_config()
        with pytest.raises(AEVSConfigError, match="not configured"):
            get_config()


class TestAEVSConfig:
    def test_frozen(self):
        configure(api_key=TEST_API_KEY)
        cfg = get_config()
        with pytest.raises(AttributeError):
            cfg.api_key = "new"  # type: ignore[misc]

    def test_repr_masks_api_key(self):
        configure(api_key=TEST_API_KEY)
        cfg = get_config()
        r = repr(cfg)
        assert cfg.api_key not in r
        assert cfg.key_id in r
        # key_secret bytes should not appear
        assert "key_secret" not in r
