import hashlib
from datetime import datetime, timezone

from aevs.config import configure, get_config
from aevs.core.signer import sign_request
from aevs.crypto.hkdf import derive_key
from aevs.crypto.hmac_auth import verify_hmac
from tests.conftest import TEST_AGENT_ID, TEST_API_KEY

FIXED_TS = datetime(2026, 3, 30, 12, 0, 0, tzinfo=timezone.utc)
PAYLOAD = b'{"seq":1,"tool_name":"search"}'


def _sign(**kwargs) -> dict[str, str]:
    configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID)
    return sign_request(get_config(), PAYLOAD, timestamp=FIXED_TS, **kwargs)


class TestSignRequest:
    def test_returns_required_headers(self):
        headers = _sign()
        assert "X-AEVS-Key-Id" in headers
        assert "X-AEVS-Timestamp" in headers
        assert "X-AEVS-Signature" in headers

    def test_key_id(self):
        headers = _sign()
        assert headers["X-AEVS-Key-Id"] == "testkey"

    def test_timestamp_format(self):
        headers = _sign()
        assert headers["X-AEVS-Timestamp"] == FIXED_TS.isoformat()

    def test_signature_is_hex(self):
        headers = _sign()
        sig = headers["X-AEVS-Signature"]
        assert len(sig) == 64
        int(sig, 16)

    def test_signature_verifiable(self):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID)
        cfg = get_config()
        headers = sign_request(cfg, PAYLOAD, timestamp=FIXED_TS)

        signing_key = derive_key(cfg.key_secret, salt="aevs-request-v1")
        payload_hash = hashlib.sha256(PAYLOAD).hexdigest()
        string_to_sign = f"{FIXED_TS.isoformat()}\n{payload_hash}"

        assert verify_hmac(
            signing_key,
            string_to_sign.encode("utf-8"),
            headers["X-AEVS-Signature"],
        )

    def test_deterministic(self):
        h1 = _sign()
        h2 = _sign()
        assert h1 == h2

    def test_different_payload_different_signature(self):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID)
        cfg = get_config()
        s1 = sign_request(cfg, b"payload-a", timestamp=FIXED_TS)
        s2 = sign_request(cfg, b"payload-b", timestamp=FIXED_TS)
        assert s1["X-AEVS-Signature"] != s2["X-AEVS-Signature"]

    def test_different_timestamp_different_signature(self):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID)
        cfg = get_config()
        ts2 = datetime(2026, 3, 30, 13, 0, 0, tzinfo=timezone.utc)
        s1 = sign_request(cfg, PAYLOAD, timestamp=FIXED_TS)
        s2 = sign_request(cfg, PAYLOAD, timestamp=ts2)
        assert s1["X-AEVS-Signature"] != s2["X-AEVS-Signature"]

    def test_auto_timestamp(self):
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID)
        headers = sign_request(get_config(), PAYLOAD)
        # Should have a valid ISO timestamp (no assertion on exact value)
        datetime.fromisoformat(headers["X-AEVS-Timestamp"])

    def test_pre_derived_key_matches(self):
        """Passing a pre-derived signing_key produces the same signature."""
        configure(api_key=TEST_API_KEY, agent_id=TEST_AGENT_ID)
        cfg = get_config()
        pre_key = derive_key(cfg.key_secret, salt="aevs-request-v1")

        h1 = sign_request(cfg, PAYLOAD, timestamp=FIXED_TS)
        h2 = sign_request(cfg, PAYLOAD, timestamp=FIXED_TS, signing_key=pre_key)
        assert h1 == h2
