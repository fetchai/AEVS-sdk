from __future__ import annotations

import base64
import json
import logging
import math
import unicodedata
from collections.abc import Mapping
from datetime import date, datetime
from typing import Any

logger = logging.getLogger("aevs")


def _normalize(
    value: Any,
    float_handling: str,
    float_precision: int,
) -> Any:
    """Recursively normalize a value for deterministic JSON serialization."""
    if value is None:
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    # bool before int — isinstance(True, int) is True in Python
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            logger.warning(
                "AEVS: float value %r is not valid JSON — replacing with null in receipt. "
                "Fix: replace NaN/inf with None or a string before passing to the tool.",
                value,
            )
            return None
        if float_handling == "raise":
            logger.warning(
                "AEVS: float value %r encountered in strict mode — "
                "converting to decimal string instead of raising. "
                "Fix: convert floats to int or string before passing to the tool.",
                value,
            )
        return f"{value:.{float_precision}f}"
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            str(k): _normalize(v, float_handling, float_precision)
            for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [_normalize(v, float_handling, float_precision) for v in value]
    return str(value)


def canonical_json(
    obj: Mapping[str, Any],
    *,
    float_handling: str = "decimal_string",
    float_precision: int = 6,
) -> bytes:
    """Serialize a dict to canonical JSON bytes.

    Canonical form: sorted keys, minimal whitespace, deterministic float handling.
    Produces identical output for identical logical data across platforms.
    """
    normalized = _normalize(obj, float_handling, float_precision)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def truncate_field(
    data: Any,
    max_bytes: int,
    *,
    float_handling: str = "decimal_string",
    float_precision: int = 6,
) -> tuple[Any, bool]:
    """Return (data, truncated). If JSON size exceeds max_bytes, replace with marker."""
    try:
        encoded = canonical_json(
            {"_": data}, float_handling=float_handling, float_precision=float_precision
        )
    except Exception:
        logger.debug(
            "AEVS: canonical_json failed inside truncate_field; using marker",
            exc_info=True,
        )
        return {"_truncated": True, "_reason": "serialization_error"}, True

    if len(encoded) <= max_bytes:
        return data, False

    preview = str(data)[:500]
    return {"_truncated": True, "_original_bytes": len(encoded), "_preview": preview}, True
