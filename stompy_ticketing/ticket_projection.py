"""Ticket row presentation, including live-only lease fields on all readers."""

import json

from stompy_ticketing.leases import live_claim_fields
from stompy_ticketing.models import TicketResponse


def _json(value):
    if value:
        try:
            return json.loads(value)
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def row_to_response(row):
    return TicketResponse(
        id=row["id"],
        title=row["title"],
        description=row.get("description"),
        type=row["type"],
        status=row["status"],
        priority=row["priority"],
        assignee=row.get("assignee"),
        tags=_json(row.get("tags")),
        metadata=_json(row.get("metadata")),
        session_id=row.get("session_id"),
        created_by=row.get("created_by"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        closed_at=row.get("closed_at"),
        archived_at=row.get("archived_at"),
        **live_claim_fields(row),
    )


def status_change(ticket):
    """Preserve the compact transition payload independently of history order."""
    status_rows = [
        h for h in getattr(ticket, "history", []) if h.field_name == "status"
    ]
    latest = max(status_rows, key=lambda h: (h.changed_at or 0, h.id), default=None)
    return {
        "id": ticket.id,
        "title": ticket.title,
        "type": ticket.type,
        "status": ticket.status,
        "previous_status": latest.old_value if latest else None,
        "closed_at": ticket.closed_at,
        "updated_at": ticket.updated_at,
    }


#: How much of an appended section its history row keeps on the MCP get door.
APPENDED_HEAD_CHARS = 100
_ELLIPSIS = "..."


def get_view(ticket):
    """The MCP get door's record, each fact once (STOMPY-3293). In place: the
    record is the one get_ticket just built for this call.

    - a description_appended row keeps the section's length and its first
      APPENDED_HEAD_CHARS characters; the whole section is in description;
    - a ticket-link row keeps ids, direction, relation and peer_*, not the
      source/target title and status pairs (one of them is this ticket, the
      other is peer_* again);
    - a context-link row drops ticket_title / ticket_status, this ticket's.
    The REST detail keeps the whole shape.
    """
    for h in ticket.history:
        if h.field_name == "description_appended" and h.new_value is not None:
            h.new_value_chars = len(h.new_value)
            if len(h.new_value) > APPENDED_HEAD_CHARS:
                h.new_value = h.new_value[:APPENDED_HEAD_CHARS] + _ELLIPSIS
    for link in ticket.links:
        if getattr(link, "peer_id", None) is not None:
            link.source_title = link.source_status = None
            link.target_title = link.target_status = None
    for c in ticket.context_links:
        if not isinstance(c, str):
            c.ticket_title = c.ticket_status = None
    return ticket
