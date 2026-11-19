"""Lossless structured responses, independent of the host (STOMPY-3757)."""

import json


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
