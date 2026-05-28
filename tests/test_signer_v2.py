"""Tests for SDK ECDSA P-256 v2 signing."""

from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from datetime import datetime, timezone

from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization

from aevs.config import AEVSConfig, _parse_api_key
from aevs.core.signer import sign_request
from aevs.crypto.ecdsa import _private_key_from_hex


def _make_v2_config() -> tuple[AEVSConfig, str]:
    """Generate a v2 keypair and return (config, public_key_b64)."""
    import base64

    pk = ec.generate_private_key(ec.SECP256R1())
    priv_hex = pk.private_numbers().private_value.to_bytes(32, "big").hex()
    pub_der = pk.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    pub_b64 = base64.b64encode(pub_der).decode()
    api_key = f"aevs_sk2_testkey_{priv_hex}"
    cfg = AEVSConfig(
        api_key=api_key,
        key_id="testkey",
        key_secret=bytes.fromhex(priv_hex),
        agent_id="00000000-0000-0000-0000-000000000001",
        auth_version=2,
    )
    return cfg, pub_b64


class TestParseApiKeyV2:
    def test_v1_key(self):
        kid, secret, ver = _parse_api_key("aevs_sk_abc12345_" + "ab" * 32)
        assert ver == 1
        assert kid == "abc12345"

    def test_v2_key(self):
        kid, secret, ver = _parse_api_key("aevs_sk2_xyz99999_" + "cd" * 32)
        assert ver == 2
        assert kid == "xyz99999"

    def test_invalid_key(self):
        try:
            _parse_api_key("invalid_key")
            assert False, "Should have raised"
        except Exception:
            pass


class TestSignRequestV2:
    def test_returns_ecdsa_signature(self):
        cfg, _ = _make_v2_config()
        headers = sign_request(cfg, b'{"test": 1}')
        assert len(headers["X-AEVS-Signature"]) == 128
        assert headers["X-AEVS-Key-Id"] == "testkey"

    def test_different_payloads_differ(self):
        cfg, _ = _make_v2_config()
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        h1 = sign_request(cfg, b'{"a":1}', timestamp=ts)
        h2 = sign_request(cfg, b'{"b":2}', timestamp=ts)
        assert h1["X-AEVS-Signature"] != h2["X-AEVS-Signature"]

    def test_v1_produces_hmac(self):
        cfg = AEVSConfig(
            api_key="aevs_sk_test_" + "ab" * 32,
            key_id="test",
            key_secret=bytes.fromhex("ab" * 32),
            agent_id="00000000-0000-0000-0000-000000000001",
            auth_version=1,
        )
        headers = sign_request(cfg, b'{"test": 1}')
        assert len(headers["X-AEVS-Signature"]) == 64
