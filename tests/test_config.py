from pathlib import Path

import pytest

from aevs.config import _parse_api_key, _validate_agent_id, configure, get_config, reset_config
from aevs.exceptions import AEVSConfigError
from tests.conftest import TEST_AGENT_ID, TEST_API_KEY, TEST_KEY_ID, TEST_KEY_SECRET


class TestParseApiKey:
    def test_valid_key(self):
        key_id, secret, ver = _parse_api_key(TEST_API_KEY)
        assert key_id == TEST_KEY_ID
        assert secret == TEST_KEY_SECRET
        assert ver == 1

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
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID)
        cfg = get_config()
        assert cfg.key_id == TEST_KEY_ID
        assert cfg.key_secret == TEST_KEY_SECRET
        assert cfg.agent_id == TEST_AGENT_ID
        assert cfg.base_url == "https://api.aevs.fetch.ai/v1"
        assert cfg.float_handling == "decimal_string"

    def test_custom_values(self):
        custom_agent_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab"
        configure(
            api_key=TEST_API_KEY,
            agent_id=custom_agent_id,
            base_url="https://api.example.com/v1/",
            signing_timeout_ms=5000,
            float_precision=10,
        )
        cfg = get_config()
        assert cfg.agent_id == custom_agent_id
        assert cfg.base_url == "https://api.example.com/v1"  # trailing slash stripped
        assert cfg.signing_timeout_ms == 5000
        assert cfg.float_precision == 10

    def test_last_wins(self):
        id1 = "11111111-1111-4111-8111-111111111111"
        id2 = "22222222-2222-4222-8222-222222222222"
        configure(api_key=TEST_API_KEY, agent_id=id1)
        configure(api_key=TEST_API_KEY, agent_id=id2)
        assert get_config().agent_id == id2

    def test_bad_float_handling_autocorrects(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, float_handling="invalid")
        assert "float_handling" in caplog.text
        assert get_config().float_handling == "decimal_string"

    def test_negative_precision_autocorrects(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, float_precision=-1)
        assert "float_precision" in caplog.text
        assert get_config().float_precision == 6

    def test_zero_timeout_autocorrects(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, signing_timeout_ms=0)
        assert "signing_timeout_ms" in caplog.text
        assert get_config().signing_timeout_ms == 2000

    def test_buffer_path_accepts_string(self):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, buffer_path="/tmp/test.db")
        assert get_config().buffer_path == Path("/tmp/test.db")

    def test_max_buffer_records_default(self):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID)
        assert get_config().max_buffer_records == 10_000

    def test_max_buffer_records_custom(self):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, max_buffer_records=500)
        assert get_config().max_buffer_records == 500

    def test_zero_max_buffer_records_autocorrects(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, max_buffer_records=0)
        assert "max_buffer_records" in caplog.text
        assert get_config().max_buffer_records == 10_000

    def test_negative_max_buffer_records_autocorrects(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID, max_buffer_records=-1)
        assert "max_buffer_records" in caplog.text
        assert get_config().max_buffer_records == 10_000

    def test_configure_while_enabled_warns(self, tmp_path, caplog):
        """configure() must warn and keep existing config while AEVS is enabled."""
        import aevs

        configure(
            api_key=TEST_API_KEY,
            agent_id=TEST_AGENT_ID,
            buffer_path=str(tmp_path / "buf.db"),
        )
        aevs.enable(frameworks=["langchain"])
        try:
            with caplog.at_level("WARNING", logger="aevs"):
                configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID)
            assert "Cannot reconfigure" in caplog.text
            assert get_config() is not None
        finally:
            aevs.disable()

    def test_configure_after_disable_works(self, tmp_path):
        """configure() should work again after disable()."""
        import aevs

        configure(
            api_key=TEST_API_KEY,
            agent_id=TEST_AGENT_ID,
            buffer_path=str(tmp_path / "buf.db"),
        )
        aevs.enable(frameworks=["langchain"])
        aevs.disable()
        new_agent_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
        configure(api_key=TEST_API_KEY, agent_id=new_agent_id)
        assert get_config().agent_id == new_agent_id


class TestMissingCredentials:
    def test_no_key_warns_and_stays_unconfigured(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            configure()
        assert "api_key and agent_id" in caplog.text
        assert "aevs.fetch.ai" in caplog.text
        assert get_config() is None

    def test_only_api_key_warns_about_agent_id(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            configure(api_key=TEST_API_KEY)
        assert "agent_id" in caplog.text
        assert get_config() is None

    def test_only_agent_id_warns_about_api_key(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            configure(agent_id=TEST_AGENT_ID)
        assert "api_key" in caplog.text
        assert get_config() is None

    def test_api_key_env_var_fallback(self, monkeypatch):
        monkeypatch.setenv("AEVS_API_KEY", TEST_API_KEY)
        configure(agent_id=TEST_AGENT_ID)
        cfg = get_config()
        assert cfg.key_id == TEST_KEY_ID

    def test_agent_id_env_var_fallback(self, monkeypatch):
        monkeypatch.setenv("AEVS_AGENT_ID", TEST_AGENT_ID)
        configure(api_key=TEST_API_KEY)
        cfg = get_config()
        assert cfg.agent_id == TEST_AGENT_ID

    def test_both_env_vars_fallback(self, monkeypatch):
        monkeypatch.setenv("AEVS_API_KEY", TEST_API_KEY)
        monkeypatch.setenv("AEVS_AGENT_ID", TEST_AGENT_ID)
        configure()
        cfg = get_config()
        assert cfg.key_id == TEST_KEY_ID
        assert cfg.agent_id == TEST_AGENT_ID

    def test_explicit_values_take_precedence_over_env(self, monkeypatch):
        other_key = "aevs_sk_envkey_" + "cd" * 32
        other_agent = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaab"
        monkeypatch.setenv("AEVS_API_KEY", other_key)
        monkeypatch.setenv("AEVS_AGENT_ID", other_agent)
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID)
        cfg = get_config()
        assert cfg.key_id == TEST_KEY_ID
        assert cfg.agent_id == TEST_AGENT_ID

    def test_enable_without_configure_warns_noop(self, caplog):
        """enable() with no prior configure() warns and runs as no-op."""
        import aevs

        with caplog.at_level("WARNING", logger="aevs"):
            aevs.enable()
        assert "configure" in caplog.text
        assert not aevs._api._enabled

    def test_enable_after_missing_creds_warns_noop(self, caplog):
        """configure() with no creds + enable() = warnings, no crash."""
        import aevs

        with caplog.at_level("WARNING", logger="aevs"):
            configure()
            aevs.enable()
        assert "must be set" in caplog.text
        assert not aevs._api._enabled


class TestAgentIdValidation:
    def test_valid_v4_uuid(self):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID)
        assert get_config().agent_id == TEST_AGENT_ID

    def test_rejects_non_uuid_string(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            configure(api_key=TEST_API_KEY, agent_id="not-a-uuid-at-all")
        assert "valid UUID" in caplog.text
        assert get_config() is None

    def test_rejects_prefixed_identifier(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            configure(api_key=TEST_API_KEY, agent_id="agt_12345")
        assert "not a prefixed" in caplog.text
        assert get_config() is None

    def test_rejects_agent_prefixed_identifier(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            configure(api_key=TEST_API_KEY, agent_id="agent_12345")
        assert "not a prefixed" in caplog.text
        assert get_config() is None

    def test_hints_uuid_without_dashes(self, caplog):
        hex32 = "a" * 32
        with caplog.at_level("WARNING", logger="aevs"):
            configure(api_key=TEST_API_KEY, agent_id=hex32)
        assert "without dashes" in caplog.text
        assert get_config() is None

    def test_empty_string_treated_as_missing(self, caplog):
        """Empty agent_id is treated as missing, not validated."""
        with caplog.at_level("WARNING", logger="aevs"):
            configure(api_key=TEST_API_KEY, agent_id="")
        assert "agent_id" in caplog.text
        assert get_config() is None

    def test_validate_agent_id_directly(self):
        _validate_agent_id(TEST_AGENT_ID)

    def test_validate_agent_id_rejects_garbage(self):
        with pytest.raises(AEVSConfigError, match="valid UUID"):
            _validate_agent_id("not-a-uuid")


class TestGetConfig:
    def test_returns_none_before_configure(self, caplog):
        with caplog.at_level("WARNING", logger="aevs"):
            result = get_config()
        assert result is None
        assert "configure" in caplog.text

    def test_reset_clears(self, caplog):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID)
        reset_config()
        with caplog.at_level("WARNING", logger="aevs"):
            result = get_config()
        assert result is None


class TestAEVSConfig:
    def test_frozen(self):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID)
        cfg = get_config()
        with pytest.raises(AttributeError):
            cfg.api_key = "new"  # type: ignore[misc]

    def test_repr_masks_api_key(self):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID)
        cfg = get_config()
        r = repr(cfg)
        assert cfg.api_key not in r
        assert cfg.key_id in r
        # key_secret bytes should not appear
        assert "key_secret" not in r
