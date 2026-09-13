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
