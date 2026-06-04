from __future__ import annotations

import dataclasses
import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aevs.exceptions import AEVSConfigError

logger = logging.getLogger("aevs")

_API_KEY_RE_V1 = re.compile(r"^aevs_sk_([a-zA-Z0-9]+)_([a-fA-F0-9]+)$")
_API_KEY_RE_V2 = re.compile(r"^aevs_sk2_([a-zA-Z0-9]+)_([a-fA-F0-9]+)$")

_VALID_FLOAT_HANDLING = {"decimal_string", "raise"}
_VALID_RECEIPT_VISIBILITY = {"public", "private", "proof_only"}


@dataclass(frozen=True, slots=True)
class AEVSConfig:
    """Immutable SDK configuration. Created via `configure()`."""

    api_key: str
    key_id: str
    key_secret: bytes
    agent_id: str
    auth_version: int = 1
    base_url: str = "https://api.aevs.fetch.ai/v1"
    signing_timeout_ms: int = 2000
    float_handling: str = "decimal_string"
    float_precision: int = 6
    max_payload_bytes: int = 1_048_576
    buffer_path: Path = Path("~/.aevs/buffer.db")
    max_buffer_records: int = 10_000
    drain_interval_ms: int = 5_000
    max_reference_entries: int = 1_000
    receipt_visibility: str = "private"

    def __repr__(self) -> str:
        masked = self.api_key[:12] + "..." if len(self.api_key) > 16 else "***"
        return (
            f"AEVSConfig(key_id={self.key_id!r}, agent_id={self.agent_id!r}, "
            f"base_url={self.base_url!r}, api_key={masked!r})"
        )


_MIN_SECRET_HEX_CHARS = 32  # 16 bytes = 128 bits minimum entropy


def _parse_api_key(api_key: str) -> tuple[str, bytes, int]:
    """Extract key_id, key_secret, and auth_version from the API key string.

    Returns ``(key_id, key_secret_bytes, auth_version)`` where
    *auth_version* is 1 for ``aevs_sk_`` keys and 2 for ``aevs_sk2_`` keys.
    """
    match = _API_KEY_RE_V2.match(api_key)
    if match:
        auth_version = 2
    else:
        match = _API_KEY_RE_V1.match(api_key)
        auth_version = 1

    if not match:
        raise AEVSConfigError(
            "Invalid API key format. Expected: aevs_sk_<key_id>_<hex_secret> "
            "or aevs_sk2_<key_id>_<hex_secret>"
        )
    key_id = match.group(1)
    hex_secret = match.group(2)
    if len(hex_secret) < _MIN_SECRET_HEX_CHARS:
        raise AEVSConfigError(
            f"API key secret too short — need at least "
            f"{_MIN_SECRET_HEX_CHARS // 2} bytes ({_MIN_SECRET_HEX_CHARS} hex chars), "
            f"got {len(hex_secret)} hex chars"
        )
    try:
        key_secret = bytes.fromhex(hex_secret)
    except ValueError as exc:
        raise AEVSConfigError(f"API key secret is not valid hex: {exc}") from exc
    return key_id, key_secret, auth_version


def _validate_agent_id(agent_id: str) -> None:
    """Validate that agent_id is a canonical UUID string."""
    if len(agent_id) == 32 and re.fullmatch(r"[a-fA-F0-9]+", agent_id):
        dashed = (
            f"{agent_id[:8]}-{agent_id[8:12]}-{agent_id[12:16]}-"
            f"{agent_id[16:20]}-{agent_id[20:]}"
        )
        raise AEVSConfigError(
            f"agent_id looks like a UUID without dashes — use '{dashed}'"
        )
    if agent_id.startswith(("agt_", "agent_")):
        raise AEVSConfigError(
            f"agent_id must be a UUID, not a prefixed identifier: {agent_id!r}"
        )
    try:
        parsed = uuid.UUID(agent_id)
    except (ValueError, AttributeError):
        raise AEVSConfigError(f"agent_id must be a valid UUID, got {agent_id!r}")
    if str(parsed) != agent_id.lower():
        raise AEVSConfigError(
            f"agent_id must be a canonical UUID string — use '{parsed}'"
        )


def _warn_if_insecure_remote_http(base_url: str) -> None:
    """Log when ``base_url`` uses cleartext HTTP against a non-loopback host."""
    parsed = urlparse(base_url)
    if parsed.scheme != "http":
        return
    host = (parsed.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1", ""):
        return
    logger.warning(
        "AEVS: base_url uses plaintext HTTP for host %r — prefer HTTPS in production",
        host,
    )


def _sanitize_config(config: AEVSConfig) -> AEVSConfig:
    """Validate configuration values, auto-correcting non-critical fields to defaults.

    Returns a (possibly corrected) config. Never raises — logs warnings for
    each field that was auto-corrected.
    """
    corrections: dict[str, Any] = {}

    if config.float_handling not in _VALID_FLOAT_HANDLING:
        logger.warning(
            "AEVS: float_handling must be one of %s, got %r. "
            "Using default 'decimal_string'.",
            _VALID_FLOAT_HANDLING, config.float_handling,
        )
        corrections["float_handling"] = "decimal_string"

    if config.float_precision < 0:
        logger.warning(
            "AEVS: float_precision must be non-negative (got %d). "
            "Using default value 6.",
            config.float_precision,
        )
        corrections["float_precision"] = 6

    if config.signing_timeout_ms <= 0:
        logger.warning(
            "AEVS: signing_timeout_ms must be positive (got %d). "
            "Using default value 2000.",
            config.signing_timeout_ms,
        )
        corrections["signing_timeout_ms"] = 2000

    if config.max_payload_bytes <= 0:
        logger.warning(
            "AEVS: max_payload_bytes must be positive (got %d). "
            "Using default value 1048576.",
            config.max_payload_bytes,
        )
        corrections["max_payload_bytes"] = 1_048_576

    if config.max_buffer_records <= 0:
        logger.warning(
            "AEVS: max_buffer_records must be positive (got %d). "
            "Using default value 10000.",
            config.max_buffer_records,
        )
        corrections["max_buffer_records"] = 10_000

    if config.drain_interval_ms <= 0:
        logger.warning(
            "AEVS: drain_interval_ms must be positive (got %d). "
            "Using default value 5000.",
            config.drain_interval_ms,
        )
        corrections["drain_interval_ms"] = 5_000

    if config.max_reference_entries <= 0:
        logger.warning(
            "AEVS: max_reference_entries must be positive (got %d). "
            "Using default value 1000.",
            config.max_reference_entries,
        )
        corrections["max_reference_entries"] = 1_000

    if config.receipt_visibility not in _VALID_RECEIPT_VISIBILITY:
        logger.warning(
            "AEVS: receipt_visibility must be one of %s, got %r. "
            "Using default 'private'.",
            _VALID_RECEIPT_VISIBILITY, config.receipt_visibility,
        )
        corrections["receipt_visibility"] = "private"

    if corrections:
        return dataclasses.replace(config, **corrections)
    return config


# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------

_global_config: AEVSConfig | None = None


def configure(
    *,
    api_key: str | None = None,
    agent_id: str | None = None,
    base_url: str = "https://api.aevs.fetch.ai/v1",
    signing_timeout_ms: int = 2000,
    float_handling: str = "decimal_string",
    float_precision: int = 6,
    max_payload_bytes: int = 1_048_576,
    buffer_path: str | Path = Path("~/.aevs/buffer.db"),
    max_buffer_records: int = 10_000,
    drain_interval_ms: int = 5_000,
    max_reference_entries: int = 1_000,
    receipt_visibility: str = "private",
) -> None:
    """Set AEVS configuration. Must be called before enable().

    *api_key* and *agent_id* fall back to ``AEVS_API_KEY`` /
    ``AEVS_AGENT_ID`` env vars.  If either is still missing the SDK
    logs a warning and enters no-op mode (never crashes the host).
    """
    global _global_config

    _api = sys.modules.get("aevs._api")
    if _api is not None and getattr(_api, "_enabled", False):
        logger.warning(
            "AEVS: Cannot reconfigure while AEVS is enabled. "
            "Call aevs.disable() first. Configuration unchanged."
        )
        return

    resolved_key = api_key or os.environ.get("AEVS_API_KEY")
    resolved_agent_id = agent_id or os.environ.get("AEVS_AGENT_ID")

    missing = []
    if not resolved_key:
        missing.append("api_key")
    if not resolved_agent_id:
        missing.append("agent_id")
    if missing:
        opts = " and ".join(missing)
        envs = " / ".join(
            f"AEVS_{m.upper()}" for m in missing
        )
        logger.warning(
            "The %s client option must be set either by passing %s to "
            "aevs.configure() or by setting the %s environment variable. "
            "Get your credentials at https://aevs.fetch.ai",
            opts, opts, envs,
        )
        _global_config = None
        return

    if resolved_key is None or resolved_agent_id is None:
        logger.warning(
            "AEVS: resolved credentials are unexpectedly None. "
            "AEVS will run in no-op mode — no receipts will be captured."
        )
        _global_config = None
        return

    try:
        key_id, key_secret, auth_version = _parse_api_key(resolved_key)
        _validate_agent_id(resolved_agent_id)
    except AEVSConfigError as exc:
        logger.warning(
            "AEVS: %s AEVS will run in no-op mode — no receipts will be captured.",
            exc,
        )
        _global_config = None
        return

    resolved_visibility = receipt_visibility or os.environ.get("AEVS_RECEIPT_VISIBILITY", "private")
    config = AEVSConfig(
        api_key=resolved_key,
        key_id=key_id,
        key_secret=key_secret,
        agent_id=resolved_agent_id,
        auth_version=auth_version,
        base_url=base_url.rstrip("/"),
        signing_timeout_ms=signing_timeout_ms,
        float_handling=float_handling,
        float_precision=float_precision,
        max_payload_bytes=max_payload_bytes,
        buffer_path=Path(buffer_path),
        max_buffer_records=max_buffer_records,
        drain_interval_ms=drain_interval_ms,
        max_reference_entries=max_reference_entries,
        receipt_visibility=resolved_visibility.lower(),
    )
    config = _sanitize_config(config)
    _warn_if_insecure_remote_http(config.base_url)
    _global_config = config


def get_config() -> AEVSConfig | None:
    """Return the current config, or None if not configured.

    When None is returned the SDK operates in no-op mode — no receipts
    are captured but the host agent is never interrupted.
    """
    if _global_config is None:
        logger.warning(
            "AEVS: aevs.configure() has not been called or configuration failed. "
            "AEVS is in no-op mode — no receipts will be captured."
        )
    return _global_config


def reset_config() -> None:
    """Clear global config. For testing only."""
    global _global_config
    _global_config = None
