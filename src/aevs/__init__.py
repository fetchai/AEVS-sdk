from aevs._version import __version__
from aevs.config import configure

__all__ = [
    "__version__",
    "configure",
    "enable",
    "disable",
    "flush",
    "is_healthy",
    "get_reference_id",
    "get_reference_ids",
    "clear_reference_ids",
]


def enable(*, frameworks: list[str] | None = None) -> None:
    """Detect installed frameworks and patch them to intercept tool calls."""
    from aevs._api import enable as _enable

    _enable(frameworks=frameworks)


def disable() -> None:
    """Unpatch all frameworks, restore original behavior."""
    from aevs._api import disable as _disable

    _disable()


def flush() -> None:
    """Force-send all buffered receipts to the backend."""
    from aevs._api import flush as _flush

    _flush()


def is_healthy(*, threshold: int = 3) -> bool:
    """Return ``False`` when the receipt buffer has had *threshold* or more
    consecutive write failures — indicating a sustained storage problem.

    Safe to call at any time; never raises.
    """
    from aevs._api import is_healthy as _is_healthy

    return _is_healthy(threshold=threshold)


def get_reference_id(lookup_id: str) -> str | None:
    """Return the AEVS reference_id for a run_id or tool_call_id."""
    from aevs._api import get_reference_id as _get

    return _get(lookup_id)


def get_reference_ids(*, clear: bool = False) -> list[dict[str, str | int | None]]:
    """Return all reference entries recorded since the last clear.

    Pass ``clear=True`` to empty the registry after reading (recommended
    for per-request web applications).
    """
    from aevs._api import get_reference_ids as _get

    return _get(clear=clear)


def clear_reference_ids() -> None:
    """Drop all stored reference entries."""
    from aevs._api import clear_reference_ids as _clear

    _clear()
