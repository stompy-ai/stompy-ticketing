"""Ticket reference coercion and display-id formatting.

A ticket reference arrives as one of:
- int             → a ticket id in the CURRENT project (legacy form)
- "1311"          → digits-only string: same as int (clients that stringify
                    ints must not fall into the prefix parser — review #8)
- "STOMPY-1311"   → prefixed display id: globally unambiguous; the prefix
                    resolves to a project via the host-injected resolver.
                    Case-insensitive on input, canonical form is UPPER.

Display ids are ``{PREFIX}-{seq}``. The underlying per-project SERIAL id is
unchanged — the prefix supplies global uniqueness, the seq stays small and
chronological within its project.
"""

import re
from typing import Callable, Optional, Tuple, Union

# Prefix: starts with a letter, alnum, max 10 — mirrors the host-side CHECK
# constraint on project_metadata.ticket_prefix. Seq: plain digits.
TICKET_REF_RE = re.compile(r"^([A-Za-z][A-Za-z0-9]{0,9})-([0-9]{1,9})$", re.ASCII)
_DIGITS_RE = re.compile(r"^[0-9]{1,9}$", re.ASCII)

# Id range parity across ALL input forms: SERIAL ids are >= 1, and the
# string forms cap at 9 digits — ints get the same bounds (review #3).
_MAX_TICKET_ID = 999_999_999


class TicketRefError(ValueError):
    """A ticket reference that cannot be understood or resolved."""


def coerce_ticket_ref(
    value: Union[int, str, None],
    current_project: Optional[str],
    resolve_prefix_func: Optional[Callable[[str], Optional[str]]] = None,
) -> Tuple[Optional[str], Optional[int]]:
    """Normalize a ticket reference to ``(project, int_id)``.

    Returns ``(current_project, id)`` for int/digit forms — the passed
    project is ECHOED unchanged, so ``result_project != passed_project``
    holds exactly when a prefix resolved elsewhere (call sites rely on this
    to detect overrides). Prefixed forms return ``(resolved_project, id)``. ``(current_project, None)``
    when value is None (caller decides whether the id was required).

    Raises TicketRefError with an actionable message for unparseable refs and
    unknown prefixes — never silently mis-parses.
    """
    if value is None:
        return current_project, None
    # bool subclasses int: ticket_id=true would silently reference ticket 1
    # (review #1). Gate types explicitly so ALL garbage funnels through
    # TicketRefError, never AttributeError (review #2).
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TicketRefError(
            f"Unrecognized ticket reference {value!r}. Use a numeric id "
            "(1311) or a prefixed id (STOMPY-1311)."
        )
    if isinstance(value, int):
        if not 1 <= value <= _MAX_TICKET_ID:
            raise TicketRefError(
                f"Ticket id {value} out of range (1..{_MAX_TICKET_ID})."
            )
        return current_project, value
    ref = value.strip()
    if _DIGITS_RE.match(ref):
        seq = int(ref)
        if seq < 1:
            raise TicketRefError(f"Ticket id {seq} out of range (1..{_MAX_TICKET_ID}).")
        return current_project, seq
    m = TICKET_REF_RE.match(ref)
    if not m:
        raise TicketRefError(
            f"Unrecognized ticket reference {value!r}. Use a numeric id "
            "(1311) or a prefixed id (STOMPY-1311)."
        )
    prefix, seq = m.group(1).upper(), int(m.group(2))
    if resolve_prefix_func is None:
        raise TicketRefError(
            f"Prefixed ticket ids ({prefix}-{seq}) are not supported by this "
            "host — pass a numeric id with the project parameter instead."
        )
    project = resolve_prefix_func(prefix)
    if not project:
        raise TicketRefError(
            f"Unknown ticket prefix {prefix!r}. Check the id, or pass a "
            "numeric id with the project parameter."
        )
    return project, seq


def format_display_id(prefix: Optional[str], ticket_id: int) -> str:
    """``PREFIX-123`` when a prefix is known, else the plain numeric string.

    Prefix is upcased — canonical form is UPPER regardless of storage.
    """
    return f"{prefix.upper()}-{ticket_id}" if prefix else str(ticket_id)
