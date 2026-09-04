"""Shared error envelope for stompy-ticketing's MCP tools (STOMPY-1879).

Replicates the host's claude_mcp_utils.mcp_error / recoverable_error shape
EXACTLY (same keys: success, error, message, and — for a recoverable error
— recovery.{can_retry, steps}, plus any extra details merged in): this
package cannot import claude_mcp_utils — it declares no dependency on the
host and is pip-installed standalone, pinned by git hash — so the shape is
replicated here rather than shared, and pinned by a contract test in BOTH
repos asserting the same key set.

Before this, every bare tool-level error in mcp_tools.py was
`{"error": "..."}` — no code, no recovery guidance, unlike every core
tool's structured shape. An agent cannot write one handler for two shapes,
so it wrote none.
"""

import json
from typing import Any, Dict, List, Optional


def mcp_error(code: str, message: str, details: Optional[Dict[str, Any]] = None) -> str:
    """`{"success": false, "error": code, "message": ..., **details}` —
    for an error with no useful "try this next" guidance (a caller-input
    mistake like a missing required field)."""
    payload: Dict[str, Any] = {"success": False, "error": code, "message": message}
    if details:
        payload.update(details)
    return json.dumps(payload, indent=2)


def recoverable_error(
    code: str,
    message: str,
    recovery_steps: List[str],
    details: Optional[Dict[str, Any]] = None,
) -> str:
    """`{"success": false, "error": code, "message": ..., "recovery":
    {"can_retry": true, "steps": recovery_steps}, **details}`."""
    payload: Dict[str, Any] = {
        "success": False,
        "error": code,
        "message": message,
        "recovery": {"can_retry": True, "steps": recovery_steps},
    }
    if details:
        payload.update(details)
    return json.dumps(payload, indent=2)


def not_found_error(entity: str, ref: Any) -> str:
    """The most common shape in this plugin: a ticket ref that doesn't
    resolve to a row."""
    return recoverable_error(
        "NOT_FOUND",
        f"{entity} {ref!r} not found",
        ["Check the ticket ID or ref is correct", "ticket_search() to find the right ticket"],
        {"ref": str(ref)},
    )
