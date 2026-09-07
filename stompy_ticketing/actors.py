"""An actor value fit to PRINT (STOMPY-1991).

`tickets.created_by` and `ticket_history.changed_by` hold an identity —
str(internal_id) — since STOMPY-1594. Rows written before the host stopped
stamping emails hold a literal ADDRESS, and a ticket payload is readable by
every member of a shared project, so those rows published the filer's email
to everyone with read access on both doors.

The host owns the display RULE (`attribution_display`: display name, else
handle, else the reader's OWN address, else nothing). This module owns only
the refusal: a value carrying an `@` is never printed raw. It is replaced by
what the host is willing to show this reader, and by a placeholder when the
host will show nothing.

A numeric id passes through untouched — it names nobody by itself, and
callers key on it.
"""

from typing import Any, Mapping, Optional

ADDRESS_PLACEHOLDER = "a member"

__all__ = ["ADDRESS_PLACEHOLDER", "safe_actor", "redact_actors"]


def safe_actor(value: Any, names: Optional[Mapping[str, str]] = None) -> Any:
    """`value` if it is safe to print, else the host's name for it, else the
    placeholder."""
    if not isinstance(value, str) or "@" not in value:
        return value
    return (names or {}).get(value) or ADDRESS_PLACEHOLDER


def redact_actors(ticket, names: Optional[Mapping[str, str]] = None):
    """Apply :func:`safe_actor` to every actor field a ticket carries.

    Returns the ticket, so a caller can wrap a return value in it. Tolerates
    anything that is not a ticket (None, a dict, a card list element without
    these fields) — a read must never fail over attribution.
    """
    if getattr(ticket, "created_by", None):
        ticket.created_by = safe_actor(ticket.created_by, names)
    for entry in getattr(ticket, "history", None) or []:
        if getattr(entry, "changed_by", None):
            entry.changed_by = safe_actor(entry.changed_by, names)
    return ticket
