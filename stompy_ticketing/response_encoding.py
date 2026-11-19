"""Lossless structured responses, independent of the host (STOMPY-3757)."""

import json
from typing import Any


def toon_encode(data):
    """Keep compact TOON only when decoding it preserves every value."""
    try:
        from toon import decode, encode

        encoded = encode(data)
        if decode(encoded) == data:
            return encoded
    except Exception:
        # Both codec failures and a decoded-but-changed value use JSON.
        pass
    return json.dumps(data, default=str)


def _omit_empty(obj: Any) -> Any:
    """Drop None values and empty collections, recursively (STOMPY-1908).
    Eight keys of `null` / `[]` per ticket said nothing; a caller that needs
    history asks for it. Zero and False are values and stay."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            v = _omit_empty(v)
            if v is None or (isinstance(v, (list, dict)) and not v):
                continue
            out[k] = v
        return out
    if isinstance(obj, list):
        return [_omit_empty(v) for v in obj]
    return obj
