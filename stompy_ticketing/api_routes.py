"""FastAPI REST API routes for stompy-ticketing.

Ticket sub-resources, links and board views. The router is mounted at
/projects/{name}/tickets by the host application.

The HOST owns GET/POST /projects/{name}/tickets (list + create): its handlers
go through the one project-access resolver (shared membership, write-role
gate). The plugin's own pair was mounted AFTER the host's and never ran —
STOMPY-1589 removed it in 0.7.1 rather than keep a dead twin.

The router receives the DB connection via FastAPI dependency injection.
"""

from typing import Callable, List, Optional

from fastapi import APIRouter, HTTPException, Query, status

from stompy_ticketing.models import (
    BatchCloseRequest,
    BatchMoveRequest,
    BatchOperationResult,
    BoardView,
    SearchResult,
    TicketLinkCreate,
    TicketLinkResponse,
    TicketResponse,
    TicketTransition,
    TicketUpdate,
)
from stompy_ticketing.service import InvalidTransitionError, TicketService

router = APIRouter(prefix="/projects/{name}/tickets", tags=["Tickets"])

# Service singleton
_service = TicketService()

# These will be set by the plugin registration to provide DB access.
# get_db_func(project, require_write=...) returns a context manager that
# yields a DB connection. require_write=True asks the HOST to enforce its
# shared-project write-role gate before handing out the connection
# (STOMPY-1423/1432) — reads pass False, mutations True, ALWAYS explicit.
_get_db_for_project: Optional[Callable] = None
_resolve_schema: Optional[Callable] = None
_on_ticket_write: Optional[Callable] = None


def configure_routes(
    get_db_func: Callable,
    resolve_schema_func: Optional[Callable] = None,
    cache_invalidator_func: Optional[Callable] = None,
) -> None:
    """Configure the router with database access functions.

    Args:
        get_db_func: Function(project, require_write=bool) -> context-manager
            DB connection. require_write=True enforces the host's
            shared-project write-role gate (STOMPY-1423/1432); every call
            site in this module passes it explicitly.
        resolve_schema_func: Function(name) -> schema name. If None, uses name directly.
        cache_invalidator_func: Optional function(project) called after ticket
            writes to invalidate REST response caches. No-op if None.
    """
    global _get_db_for_project, _resolve_schema, _on_ticket_write
    _get_db_for_project = get_db_func
    _resolve_schema = resolve_schema_func
    _on_ticket_write = cache_invalidator_func


def _invalidate_ticket_cache(project: str) -> None:
    """Fire-and-forget ticket cache invalidation."""
    if _on_ticket_write is not None:
        try:
            _on_ticket_write(project)
        except Exception:
            pass  # Cache invalidation is best-effort


def _get_schema(name: str) -> str:
    """Resolve project name to schema name."""
    return _resolve_schema(name) if _resolve_schema else name


def _require_db():
    """Ensure DB access is configured."""
    if _get_db_for_project is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Ticketing plugin DB access not configured",
        )


# --------------------------------------------------------------------------- #
# Ticket CRUD                                                                 #
# --------------------------------------------------------------------------- #


@router.get("/board", response_model=BoardView)
async def board_view(
    name: str,
    view: str = Query("kanban"),
    type: Optional[str] = Query(None),
    ticket_status: Optional[str] = Query(None, alias="status"),
    include_terminal: bool = Query(False),
    include_archived: bool = Query(False),
):
    """Get a kanban or summary board view."""
    _require_db()
    schema = _get_schema(name)
    with _get_db_for_project(name, require_write=False) as conn:
        return _service.board_view(
            conn, schema,
            type_filter=type,
            view=view,
            status_filter=ticket_status,
            include_terminal=include_terminal,
            include_archived=include_archived,
        )


@router.get("/search", response_model=SearchResult)
async def search_tickets(
    name: str,
    query: str = Query(...),
    type: Optional[str] = Query(None),
    ticket_status: Optional[str] = Query(None, alias="status"),
    limit: int = Query(20, ge=1, le=100),
    include_archived: bool = Query(False),
    fields: str = Query("card", pattern="^(card|full)$"),
):
    """Full-text search tickets. Rows are cards (no description body, a
    bounded description_preview) unless fields=full (STOMPY-1923)."""
    _require_db()
    schema = _get_schema(name)
    with _get_db_for_project(name, require_write=False) as conn:
        return _service.search_tickets(
            conn, schema, query,
            type_filter=type,
            status_filter=ticket_status,
            limit=limit,
            include_archived=include_archived,
            fields=fields,
        )


@router.post("/archive")
async def archive_tickets(name: str):
    """Manually trigger archival of stale closed tickets."""
    _require_db()
    schema = _get_schema(name)
    with _get_db_for_project(name, require_write=True) as conn:
        count = _service.archive_stale_tickets(conn, schema)
    if count > 0:
        _invalidate_ticket_cache(name)
    return {"status": "archived", "count": count}


# --------------------------------------------------------------------------- #
# Batch Operations                                                             #
# --------------------------------------------------------------------------- #


@router.post("/batch/move", response_model=BatchOperationResult)
async def batch_move(name: str, body: BatchMoveRequest):
    """Move multiple tickets to a target status in one call.

    Two-phase: default is preview (confirm=false), set confirm=true to execute.
    Automatically validates state machine transitions per ticket.
    Max 50 tickets per call.
    """
    _require_db()
    schema = _get_schema(name)
    with _get_db_for_project(name, require_write=True) as conn:
        result = _service.batch_transition(
            conn, schema, body.ticket_ids, body.status,
            confirm=body.confirm,
            changed_by=body.note,
            reason=body.reason,
            revisit_by=body.revisit_by,
        )
    if body.confirm:
        _invalidate_ticket_cache(name)
    return result


@router.post("/batch/close", response_model=BatchOperationResult)
async def batch_close(name: str, body: BatchCloseRequest):
    """Close multiple tickets in one call.

    Two-phase: default is preview (confirm=false), set confirm=true to execute.
    Walks each ticket through intermediate state transitions to reach terminal status.
    Max 50 tickets per call.
    """
    _require_db()
    schema = _get_schema(name)
    with _get_db_for_project(name, require_write=True) as conn:
        result = _service.batch_close(
            conn, schema, body.ticket_ids,
            confirm=body.confirm,
            changed_by=body.note,
        )
    if body.confirm:
        _invalidate_ticket_cache(name)
    return result


@router.get("/tags")
async def list_tags(
    name: str,
    include_archived: bool = Query(False),
):
    """List all unique tags with usage counts across tickets."""
    _require_db()
    schema = _get_schema(name)
    with _get_db_for_project(name, require_write=False) as conn:
        tags = _service.list_tags(conn, schema, include_archived=include_archived)
        return {"tags": tags, "total": len(tags)}


@router.get("/{ticket_id}", response_model=TicketResponse)
async def get_ticket(name: str, ticket_id: int):
    """Get a ticket by ID with history and links."""
    _require_db()
    schema = _get_schema(name)
    with _get_db_for_project(name, require_write=False) as conn:
        result = _service.get_ticket(conn, schema, ticket_id)
        if not result:
            raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
        return result


@router.put("/{ticket_id}", response_model=TicketResponse)
async def update_ticket(name: str, ticket_id: int, body: TicketUpdate):
    """Update ticket fields (not status - use /move for that)."""
    _require_db()
    schema = _get_schema(name)
    with _get_db_for_project(name, require_write=True) as conn:
        result = _service.update_ticket(conn, schema, ticket_id, body)
        if not result:
            raise HTTPException(status_code=404, detail=f"Ticket {ticket_id} not found")
    _invalidate_ticket_cache(name)
    return result


@router.post("/{ticket_id}/move", response_model=TicketResponse)
async def transition_ticket(name: str, ticket_id: int, body: TicketTransition):
    """Transition a ticket to a new status via the state machine."""
    _require_db()
    schema = _get_schema(name)
    with _get_db_for_project(name, require_write=True) as conn:
        try:
            result = _service.transition_ticket(
                conn, schema, ticket_id, body.status,
                reason=body.reason, revisit_by=body.revisit_by,
            )
            if not result:
                raise HTTPException(
                    status_code=404, detail=f"Ticket {ticket_id} not found"
                )
        except InvalidTransitionError as e:
            raise HTTPException(status_code=422, detail=str(e))
    _invalidate_ticket_cache(name)
    return result


# --------------------------------------------------------------------------- #
# Ticket Links                                                                #
# --------------------------------------------------------------------------- #


@router.post("/{ticket_id}/links", response_model=TicketLinkResponse, status_code=status.HTTP_201_CREATED)
async def add_link(name: str, ticket_id: int, body: TicketLinkCreate):
    """Add a link between two tickets."""
    _require_db()
    schema = _get_schema(name)
    with _get_db_for_project(name, require_write=True) as conn:
        return _service.add_link(conn, schema, ticket_id, body)


@router.get("/{ticket_id}/links", response_model=List[TicketLinkResponse])
async def list_links(name: str, ticket_id: int):
    """List all links for a ticket."""
    _require_db()
    schema = _get_schema(name)
    with _get_db_for_project(name, require_write=False) as conn:
        return _service.list_links(conn, schema, ticket_id)


@router.delete("/{ticket_id}/links/{link_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_link(name: str, ticket_id: int, link_id: int):
    """Remove a ticket link."""
    _require_db()
    schema = _get_schema(name)
    with _get_db_for_project(name, require_write=True) as conn:
        removed = _service.remove_link(conn, schema, link_id)
        if not removed:
            raise HTTPException(status_code=404, detail=f"Link {link_id} not found")
