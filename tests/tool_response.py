"""Read supported MCP formats without TOON treating JSON text as a key."""

import json

from toon import decode as toon_decode


def parse_tool_response(text):
    """JSON first: TOON may accept JSON text without raising an exception."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return toon_decode(text)
