from __future__ import annotations

import logging
import os
import re
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from aevs.exceptions import AEVSConfigError

logger = logging.getLogger("aevs")

_API_KEY_RE = re.compile(r"^aevs_sk_([a-zA-Z0-9]+)_([a-fA-F0-9]+)$")

_VALID_FLOAT_HANDLING = {"decimal_string", "raise"}


@dataclass(frozen=True, slots=True)
class AEVSConfig:
    """Immutable SDK configuration. Created via `configure()`."""

    api_key: str
    key_id: str
    key_secret: bytes
    agent_id: str
    base_url: str = "https://api.aevs.fetch.ai/v1"
    signing_timeout_ms: int = 2000
    float_handling: str = "decimal_string"
    float_precision: int = 6
    max_payload_bytes: int = 1_048_576
    buffer_path: Path = Path("~/.aevs/buffer.db")
    max_buffer_records: int = 10_000
    drain_interval_ms: int = 5_000
    max_reference_entries: int = 1_000

    def __repr__(self) -> str:
        masked = self.api_key[:12] + "..." if len(self.api_key) > 16 else "***"
        return (
            f"AEVSConfig(key_id={self.key_id!r}, agent_id={self.agent_id!r}, "
            f"base_url={self.base_url!r}, api_key={masked!r})"
        )


_MIN_SECRET_HEX_CHARS = 32  # 16 bytes = 128 bits minimum entropy


def _parse_api_key(api_key: str) -> tuple[str, bytes]:
    """Extract key_id and key_secret from the API key string."""
    match = _API_KEY_RE.match(api_key)
    if not match:
        raise AEVSConfigError(
            f"Invalid API key format. Expected: aevs_sk_<key_id>_<hex_secret>"
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
    return key_id, key_secret


def _validate_agent_id(agent_id: str) -> None:
    """Validate that agent_id is a canonical UUID string."""
    if len(agent_id) == 32 and re.fullmatch(r"[a-fA-F0-9]+", agent_id):
        dashed = f"{agent_id[:8]}-{agent_id[8:12]}-{agent_id[12:16]}-{agent_id[16:20]}-{agent_id[20:]}"
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


def _validate_config(config: AEVSConfig) -> None:
    """Validate configuration values."""
    if config.float_handling not in _VALID_FLOAT_HANDLING:
        raise AEVSConfigError(
            f"float_handling must be one of {_VALID_FLOAT_HANDLING}, "
            f"got {config.float_handling!r}"
        )
    if config.float_precision < 0:
        raise AEVSConfigError("float_precision must be non-negative")
    if config.signing_timeout_ms <= 0:
        raise AEVSConfigError("signing_timeout_ms must be positive")
    if config.max_payload_bytes <= 0:
        raise AEVSConfigError("max_payload_bytes must be positive")
    if config.max_buffer_records <= 0:
        raise AEVSConfigError("max_buffer_records must be positive")
    if config.drain_interval_ms <= 0:
        raise AEVSConfigError("drain_interval_ms must be positive")
    if config.max_reference_entries <= 0:
        raise AEVSConfigError("max_reference_entries must be positive")


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
) -> None:
    """Set AEVS configuration. Must be called before enable().

    *api_key* and *agent_id* fall back to ``AEVS_API_KEY`` /
    ``AEVS_AGENT_ID`` env vars.  If either is still missing the SDK
    logs a warning and enters no-op mode (never crashes the host).
    """
    global _global_config

    _api = sys.modules.get("aevs._api")
    if _api is not None and getattr(_api, "_enabled", False):
        raise AEVSConfigError(
            "Cannot reconfigure while AEVS is enabled. Call aevs.disable() first."
        )

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

    key_id, key_secret = _parse_api_key(resolved_key)
    _validate_agent_id(resolved_agent_id)
    config = AEVSConfig(
        api_key=resolved_key,
        key_id=key_id,
        key_secret=key_secret,
        agent_id=resolved_agent_id,
        base_url=base_url.rstrip("/"),
        signing_timeout_ms=signing_timeout_ms,
        float_handling=float_handling,
        float_precision=float_precision,
        max_payload_bytes=max_payload_bytes,
        buffer_path=Path(buffer_path),
        max_buffer_records=max_buffer_records,
        drain_interval_ms=drain_interval_ms,
        max_reference_entries=max_reference_entries,
    )
    _validate_config(config)
    _warn_if_insecure_remote_http(config.base_url)
    _global_config = config


def get_config() -> AEVSConfig:
    """Return the current config. Raises if not configured."""
    if _global_config is None:
        raise AEVSConfigError("aevs.configure() must be called before using the SDK.")
    return _global_config


def reset_config() -> None:
    """Clear global config. For testing only."""
    global _global_config
    _global_config = None
