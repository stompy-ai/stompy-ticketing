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

THE HEURISTIC, and its blind spots, stated rather than assumed (Kimi review
of #35): the needle is a literal `@`, because that is exactly how the legacy
writer stored the value — `bug_report` passed `users.email` straight through.
It therefore over-redacts a non-address string containing `@` (the safe
direction for a privacy rule, and one no writer produces today) and does not
catch an obfuscated address that carries no `@` (`jane at example.com`),
which no writer produced either. It is matched to the data, not to a general
notion of what an address looks like.
"""

from typing import Any, Mapping, Optional

ADDRESS_PLACEHOLDER = "a member"

__all__ = ["ADDRESS_PLACEHOLDER", "safe_actor", "redact_actors", "redact_payload"]


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


_CONTAINER_FIELDS = ("tickets", "columns", "results", "links", "context_links")


def redact_payload(payload, names: Optional[Mapping[str, str]] = None, _depth: int = 0):
    """Redact every actor field ANYWHERE in a response payload.

    Per-handler redaction is how a door forgets (Kimi review of #35, and this
    PR exists because the REST door was forgotten once): a board, a search
    result and a list all carry the same tickets, so the rule belongs at the
    boundary every one of them passes through, not at three call sites.

    Walks models, lists, tuples and dicts; redacts anything carrying
    `created_by`/`history`. Depth-bounded and exception-free — a read must
    never fail over attribution.
    """
    if payload is None or _depth > 6:
        return payload
    try:
        if isinstance(payload, (list, tuple)):
            for item in payload:
                redact_payload(item, names, _depth + 1)
            return payload
        if isinstance(payload, dict):
            for value in payload.values():
                redact_payload(value, names, _depth + 1)
            return payload
        redact_actors(payload, names)
        for field in _CONTAINER_FIELDS:
            nested = getattr(payload, field, None)
            if nested is not None:
                redact_payload(nested, names, _depth + 1)
    except Exception:  # attribution must never break a read
        return payload
    return payload
