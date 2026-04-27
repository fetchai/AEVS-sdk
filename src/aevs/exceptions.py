class AEVSError(Exception):
    """Base exception for all AEVS errors."""


class AEVSConfigError(AEVSError):
    """Invalid or missing configuration."""


class AEVSSerializationError(AEVSError):
    """Failed to serialize tool inputs/outputs to canonical JSON."""


class AEVSBufferError(AEVSError):
    """Local buffer operation failure."""


class AEVSAuthError(AEVSError):
    """Authentication or signing failure."""
