"""Small MCP archival dispatch, leaving the legacy tool module bounded."""

import json

from stompy_ticketing.errors import mcp_error


def _invalidate(project):
    from stompy_ticketing.api_routes import _invalidate_ticket_cache

    _invalidate_ticket_cache(project.rsplit("/", 1)[-1])


def archive_action(
    service, conn, schema, action, ticket_id, ticket_ids, confirm, actor, project=None
):
    project = project or schema
    if action == "batch_archive":
        if ticket_id is not None or not ticket_ids:
            return mcp_error(
                "INVALID_PARAMS", "batch_archive requires ticket_ids, not ticket_id"
            )
        try:
            ids = [int(part.strip()) for part in ticket_ids.split(",")]
        except ValueError:
            return mcp_error(
                "INVALID_PARAMS", "ticket_ids must be comma-separated integers"
            )
        result = service.batch_archive(
            conn, schema, ids, confirm=confirm, changed_by=actor
        )
        if confirm and result.succeeded:
            _invalidate(project)
        return result
    if ticket_ids:
        return mcp_error(
            "INVALID_PARAMS", "Use batch_archive for ticket_ids; no sweep was run"
        )
    if ticket_id is None:
        if action == "unarchive":
            return mcp_error("INVALID_PARAMS", "unarchive requires ticket_id")
        count = service.archive_stale_tickets(conn, schema)
        if count:
            _invalidate(project)
        return json.dumps(
            {
                "status": "archived",
                "count": count,
                "message": f"Archived {count} stale ticket(s)",
            }
        )
    method = (
        service.unarchive_ticket if action == "unarchive" else service.archive_ticket
    )
    result = method(conn, schema, ticket_id, changed_by=actor)
    _invalidate(project)
    from stompy_ticketing.service import TicketService

    return {
        "status": "unarchived" if action == "unarchive" else "archived",
        "ticket": TicketService.to_card(result).model_dump(),
    }
