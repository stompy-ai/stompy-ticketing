"""MCP tool definitions for stompy-ticketing.

Provides 4 tools that replace ~33 Linear tools (~800 tokens vs ~5000):
- ticket: Primary CRUD + transitions
- ticket_link: Relationship management
- ticket_board: Dashboard view
- ticket_search: Full-text search

Registration pattern: register_ticketing_tools() takes the FastMCP instance
and helper functions from the host (Stompy), decoupling this plugin from
Stompy internals.
"""

import contextvars
import fnmatch
import json
import time as _time
from typing import Annotated, Any, Callable, List, Literal, Optional, Union

from stompy_ticketing.errors import mcp_error, not_found_error, recoverable_error
from stompy_ticketing.refs import TicketRefError, coerce_ticket_ref, format_display_id

from psycopg2 import OperationalError as _OperationalError

from stompy_ticketing.models import (
    ContextLinkCreate,
    ContextLinkType,
    LinkType,
    Priority,
    TicketCreate,
    TicketLinkCreate,
    TicketListFilters,
    TicketType,
    TicketUpdate,
)
from stompy_ticketing.actors import redact_actors
from stompy_ticketing.service import (
    InvalidTransitionError,
    ParkArgumentError,
    LinkAlreadyExistsError,
    TicketService,
)


def _toon_encode(data):
    """Encode as TOON (Token-Oriented Object Notation), JSON fallback."""
    try:
        from toon import encode
        return encode(data)
    except Exception:
        return json.dumps(data, default=str)


# Display-id prefix for the CURRENT tool call (request-scoped by contextvar —
# module globals would race concurrent calls). Set by each tool after project
# resolution; consumed by _safe_json so every response gains display_id
# without touching 20 call sites.
_display_prefix: contextvars.ContextVar = contextvars.ContextVar("_display_prefix", default=None)
# The project the current tool call is about — the other half of a ticket's
# address (STOMPY-1929). Request-scoped for the same reason as the prefix.
_url_project: contextvars.ContextVar = contextvars.ContextVar("_url_project", default=None)

# Addresses a whole payload: (payload, project) -> payload, mutated in place.
# INJECTED BY THE HOST at registration (src/services/object_urls.stamp_urls),
# so BOTH the URL grammar and the shape rule — a per-object `url`, but ONE
# `url_template` on a list of cards (STOMPY-1925) — have exactly one
# implementation, shared with the REST door (STOMPY-1927). A host that predates
# 1929 injects nothing and payloads are unchanged.
_stamp_urls_func: Optional[Callable] = None


def _bind_display(project_name, prefix):
    """Bind the display prefix and project for ONE tool call. Returns the token
    pair to hand back to ``_unbind_display`` in the caller's finally block."""
    return (_display_prefix.set(prefix), _url_project.set(project_name))


def _unbind_display(token) -> None:
    prefix_token, project_token = token
    _display_prefix.reset(prefix_token)
    _url_project.reset(project_token)


def _stamp_urls(data: Any) -> Any:
    """Hand the payload to the host's stamper. Never raises into a response."""
    project = _url_project.get()
    if not _stamp_urls_func or not project:
        return data
    try:
        return _stamp_urls_func(data, project)
    except Exception:
        return data


def _decorate_display_ids(obj: Any, prefix) -> Any:
    """Recursively add display_id to anything that looks like a ticket dict.

    Runs BEFORE the host's stamper (STOMPY-1929) so a card already carries the
    display_id its address is built from.
    """
    if isinstance(obj, dict):
        if isinstance(obj.get("id"), int) and "title" in obj and "status" in obj:
            obj.setdefault("display_id", format_display_id(prefix, obj["id"]))
        for v in obj.values():
            _decorate_display_ids(v, prefix)
    elif isinstance(obj, list):
        for v in obj:
            _decorate_display_ids(v, prefix)
    return obj


def _safe_json(data: Any) -> str:
    """Serialize data as TOON for token-efficient MCP responses.

    When the request-scoped ``_display_prefix`` contextvar is set (each tool
    sets it after project resolution and resets it in ``finally``),
    ticket-shaped dicts gain ``display_id``. Outside a tool call the var is
    unset and output is undecorated — display_id is a presentation concern
    of the MCP layer, not part of the service-level ticket contract.
    """
    try:
        if hasattr(data, "model_dump"):
            data = data.model_dump()
        elif hasattr(data, "dict"):
            data = data.dict()
        _prefix = _display_prefix.get()
        if _prefix:
            data = _decorate_display_ids(data, _prefix)
        data = _stamp_urls(data)
        return _toon_encode(_omit_empty(data))
    except Exception as e:
        return json.dumps({"error": str(e)})


def _omit_empty(obj: Any) -> Any:
    """Drop None values and empty collections, recursively (STOMPY-1908).
    Eight keys of `null` / `[]` per ticket said nothing; a caller that needs
    history asks for it. Zero and False are values and stay."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            v = _omit_empty(v)
            if v is None or (isinstance(v, (list, dict)) and not v):
                continue
            out[k] = v
        return out
    if isinstance(obj, list):
        return [_omit_empty(v) for v in obj]
    return obj


def _status_change(ticket: Any) -> dict:
    """What a transition changed (STOMPY-1895): the id, the new status, the
    status it left, when. The caller supplied the body and already has it;
    action='get' has the record."""
    # The LATEST status row, whatever order history arrived in (it is
    # newest-first from _fetch_history, but this must not depend on that).
    status_rows = [h for h in getattr(ticket, "history", []) if h.field_name == "status"]
    latest = max(status_rows, key=lambda h: (h.changed_at or 0, h.id), default=None)
    previous = latest.old_value if latest else None
    return {
        "id": ticket.id,
        "title": ticket.title,
        "type": ticket.type,
        "status": ticket.status,
        "previous_status": previous,
        "closed_at": ticket.closed_at,
        "updated_at": ticket.updated_at,
    }


# STOMPY-1432: action classification driving the host's shared-project
# write-role gate (owner/contributor/admin refuses viewers).
#
# BOTH sets are declared, and the acquisitions below test membership of the
# READ set — so an action that is in NEITHER (a new one someone forgot to
# classify) acquires as a WRITE and is refused for viewers, rather than
# silently taking the read path and re-opening the hole this fixes. Kimi's
# review of #24 caught the original `action in WRITE` spelling, which failed
# OPEN on exactly that omission. The test suite pins both totality
# (READ | WRITE covers each tool's Literal) and disjointness.
TICKET_WRITE_ACTIONS = frozenset(
    {"create", "update", "append", "move", "close", "archive", "batch_move", "batch_close"}
)
TICKET_READ_ACTIONS = frozenset({"get", "list", "list_tags"})
TICKET_LINK_WRITE_ACTIONS = frozenset({"add", "remove"})
TICKET_LINK_READ_ACTIONS = frozenset({"list"})


def register_ticketing_tools(
    mcp_instance: Any,
    get_db_func: Callable,
    check_project_func: Callable,
    get_project_func: Callable,
    resolve_schema_func: Optional[Callable] = None,
    notify_resolution_func: Optional[Callable] = None,
    resolve_prefix_func: Optional[Callable] = None,
    get_prefix_func: Optional[Callable] = None,
    actor_func: Optional[Callable] = None,
    display_actors_func: Optional[Callable] = None,
    stamp_urls_func: Optional[Callable] = None,
) -> None:
    """Register ticketing MCP tools on the given FastMCP instance.

    actor_func() -> Optional[str]: the caller's identity as str(internal_id)
    (STOMPY-1594) — written as tickets.created_by on create and as
    ticket_history.changed_by on every write. display_actors_func(ids) ->
    {id: display} resolves those ids for the reader (ticket get). Both
    optional: an older host leaves the columns NULL, as before.

    Args:
        mcp_instance: FastMCP server to register tools on.
        get_db_func: Function(project=None, require_write=bool) ->
            context-manager DB connection. require_write=True enforces the
            host's shared-project write-role gate (STOMPY-1423/1432):
            viewers of a shared project are refused mutating acquisitions.
            Every call site passes it explicitly, derived from the action.
        check_project_func: Function(project=None) -> error string or None.
        get_project_func: Function(project=None) -> project name string.
        resolve_schema_func: Optional function(name) -> schema name. If None,
            uses the project name directly as the schema.
        notify_resolution_func: Optional callback(report, new_status) for bug
            resolution emails on mcp_global tickets.
        stamp_urls_func: Optional function(payload, project) -> payload that
            adds each object's canonical url, or one url_template to a list of
            cards (STOMPY-1929/1925). The host owns both the grammar and the
            shape rule; omitting it leaves payloads unchanged.
    """
    global _stamp_urls_func
    if stamp_urls_func is not None:
        _stamp_urls_func = stamp_urls_func

    service = TicketService()

    def _actor() -> Optional[str]:
        """Who is writing (STOMPY-1594) — never raises into a write."""
        try:
            return actor_func() if actor_func else None
        except Exception:
            return None

    def _decorate(ticket):
        """Fill created_by_display / changed_by_display for the reader — and
        never let an ADDRESS ride the raw field (STOMPY-1991).

        `changed_by` holds an identity (STOMPY-1594), but rows written before
        the host stopped stamping emails hold a literal address, and this
        payload is readable by every member of a shared project. Filling only
        the `*_display` fields left those addresses on the wire on both doors,
        because the host's resolver omits ids it cannot name. So an actor
        value that CONTAINS an `@` is replaced by what the host is willing to
        show this reader, and by "a member" when the host will show nothing —
        never by the address itself.

        The host owns the rule (its `attribution_display`: display name, else
        handle, else the reader's OWN address, else nothing); this only
        refuses to print what the host withheld. A numeric id is untouched:
        it names nobody by itself and callers still key on it.
        """
        ids = {ticket.created_by} | {h.changed_by for h in getattr(ticket, "history", [])}
        ids.discard(None)
        if not ids or not display_actors_func:
            return redact_actors(ticket, {})
        try:
            names = display_actors_func(sorted(ids)) or {}
        except Exception:
            return redact_actors(ticket, {})
        if ticket.created_by:
            ticket.created_by_display = names.get(ticket.created_by)
        for h in getattr(ticket, "history", []):
            if h.changed_by:
                h.changed_by_display = names.get(h.changed_by)
        # ORDER, and it is deliberate: the display fields are keyed off the
        # PRE-redaction value, because that value is what the host was asked
        # about. Redaction runs last, so the raw field can never carry an
        # address the display field already replaced (Kimi review of #35).
        return redact_actors(ticket, names)

    def _get_schema(project_name: str) -> str:
        """Resolve project name to PostgreSQL schema name."""
        return resolve_schema_func(project_name) if resolve_schema_func else project_name

    @mcp_instance.tool()
    async def ticket(
        action: Annotated[
            Literal["create", "get", "update", "append", "move", "list", "list_tags", "close", "archive", "batch_move", "batch_close"],
            "Operation to perform",
        ],
        title: Annotated[Optional[str], "Ticket title (create/update)"] = None,
        description: Annotated[Optional[str], "Ticket description (create/update)"] = None,
        type: Annotated[
            Optional[Literal["task", "bug", "feature", "decision"]],
            "Ticket type (default: task)",
        ] = None,
        priority: Annotated[
            Optional[Literal["urgent", "high", "medium", "low", "none"]],
            "Ticket priority (default: medium)",
        ] = None,
        status: Annotated[Optional[str], "Target status for move/batch_move, or filter for list"] = None,
        assignee: Annotated[Optional[str], "Assignee name"] = None,
        tags: Annotated[Optional[str], "Comma-separated tags"] = None,
        ticket_id: Annotated[
            Optional[Union[int, str]],
            "Ticket ID (get/update/move/close): numeric (1311), prefixed "
            "(STOMPY-1311), or the full ticket URL a human was sent",
        ] = None,
        ticket_ids: Annotated[
            Optional[str],
            "Comma-separated IDs (batch_move/batch_close); numeric or prefixed, all same project",
        ] = None,
        confirm: Annotated[bool, "Execute batch operation (default: preview only)"] = False,
        resolution: Annotated[
            Optional[str],
            "Terminal status enum value for close (NOT free-text). "
            "Per type: task→done|cancelled, bug→resolved|wont_fix, "
            "feature→shipped|rejected, decision→decided|deferred. "
            "Defaults to the positive terminal (done/resolved/shipped/decided)."
        ] = None,
        limit: Annotated[Optional[int], "Max tickets for list (default 20, max 200)"] = None,
        offset: Annotated[Optional[int], "Skip N tickets for list pagination"] = None,
        include_archived: Annotated[bool, "Include archived tickets in list"] = False,
        project: Annotated[Optional[str], "Project name"] = None,
        grep: Annotated[Optional[str], "Filter list results by title (fnmatch glob, e.g. 'auth*', '*bug*')"] = None,
        expected_updated_at: Annotated[
            Optional[float],
            "update only: optimistic guard — pass the updated_at you read; refused with CONFLICT if the ticket changed since (re-read and retry, or use append)",
        ] = None,
        reason: Annotated[
            Optional[str],
            "move/batch_move to status='parked' only: REQUIRED — why this is deliberately not now",
        ] = None,
        revisit_by: Annotated[
            Optional[str],
            "move/batch_move to status='parked' only: optional ISO date (YYYY-MM-DD) to look again",
        ] = None,
        fields: Annotated[
            Optional[Literal["card", "full"]],
            "list only: 'card' (default) = id/title/type/status/priority/tags/assignee/description_preview; 'full' = whole records incl. description",
        ] = None,
    ) -> str:
        """Create, update, move, close, search, and batch-manage tickets. Supports glob filter on titles (grep param). Pass project= on every call.

        Payloads: list returns CARDS (no description body; description_preview instead) — get returns the FULL record; move/close return the status change only.

        action → required params:
          create      → title (type defaults to task)
          get         → ticket_id (full record: description, history, links)
          update      → ticket_id + fields to change (+ expected_updated_at to refuse stale writes)
          append      → ticket_id + description (ATOMIC append — reports/results; never clobbers concurrent edits)
          move        → ticket_id + status (one step; a refusal names the path — close walks it) (status='parked' also needs reason; optional revisit_by)
          list        → optional filters (type/status/priority/assignee/tags/grep); fields='full' for bodies
          list_tags   → show all unique tags with usage counts (useful before filtering by tags)
          close       → ticket_id
          archive     → (none — global sweep of long-closed tickets; to shelve ONE ticket use move + status='parked')
          batch_move  → ticket_ids + status; confirm=True to execute (parked: one reason for the batch)
          batch_close → ticket_ids; confirm=True to execute

        Initial statuses: task→backlog, bug→triage, feature→proposed, decision→open.
        Terminal: task→done/cancelled, bug→resolved/wont_fix, feature→shipped/rejected, decision→decided/deferred.
        Parked ("not now", every type): from backlog/triage/confirmed/proposed/approved/open, back in one step; hidden from the board like terminals; list with status='parked'."""
        _prefix_token = None
        try:
            # Dual-format ref coercion (design 2026-07-27): a prefixed id
            # resolves its own project and OVERRIDES the project param —
            # unless the caller explicitly passed a DIFFERENT project, which
            # is a conflict we refuse rather than guess.
            try:
                _ref_project, ticket_id = coerce_ticket_ref(ticket_id, project, resolve_prefix_func)
                if _ref_project != project:
                    if project is not None:
                        return json.dumps({
                            "error": f"Ticket ref belongs to project '{_ref_project}' "
                            f"but project='{project}' was passed. Drop the project "
                            "param or use a numeric id."
                        })
                    project = _ref_project
                if ticket_ids:
                    _parts = [x.strip() for x in str(ticket_ids).split(",") if x.strip()]
                    _coerced = [coerce_ticket_ref(x, project, resolve_prefix_func) for x in _parts]
                    _projects = {pr for pr, _ in _coerced if pr is not None} or {project}
                    if len(_projects) > 1:
                        return json.dumps({
                            "error": "Batch refs span multiple projects "
                            f"({sorted(_projects)}) — batches are per-project."
                        })
                    _only = _projects.pop()
                    if _only != project:
                        if project is not None:
                            return json.dumps({
                                "error": f"Batch refs belong to project '{_only}' "
                                f"but project='{project}' was passed."
                            })
                        project = _only
                    ticket_ids = ",".join(str(i) for _, i in _coerced)
            except TicketRefError as e:
                return json.dumps({"error": str(e)})

            # Validation AFTER coercion: a prefixed ref may have just
            # supplied the project (review finding #5 — checking first
            # refused the headline BUG-188-with-no-project flow).
            project_check = check_project_func(project)
            if project_check:
                return project_check

            project_name = get_project_func(project)
            _prefix_token = _bind_display(
                project_name, get_prefix_func(project_name) if get_prefix_func else None
            )
            with get_db_func(
                project, require_write=action not in TICKET_READ_ACTIONS
            ) as conn:
                schema = _get_schema(project_name)

                if expected_updated_at is not None and action != "update":
                    return json.dumps({
                        "error": "expected_updated_at only guards action=\"update\" — "
                        "move/close have no stale-write protection yet (STOMPY-1579 follow-up)"
                    })

                if action == "create":
                    if not title:
                        return json.dumps({"error": "title is required for create"})
                    tag_list = [t.strip() for t in tags.split(",")] if tags else None
                    data = TicketCreate(
                        title=title,
                        description=description,
                        type=TicketType(type) if type else TicketType.task,
                        priority=Priority(priority) if priority else Priority.medium,
                        assignee=assignee,
                        tags=tag_list,
                    )
                    result = service.create_ticket(conn, schema, data, changed_by=_actor())
                    return _safe_json({"status": "created", "ticket": result.model_dump()})

                elif action == "get":
                    if not ticket_id:
                        return json.dumps({"error": "ticket_id is required for get"})
                    result = service.get_ticket(conn, schema, ticket_id)
                    if not result:
                        return not_found_error("Ticket", ticket_id)
                    return _safe_json(_decorate(result))

                elif action == "append":
                    if not ticket_id or not description:
                        return json.dumps({"error": "ticket_id and description are required for append"})
                    try:
                        result = service.append_description(
                            conn, schema, ticket_id, description, changed_by=_actor()
                        )
                    except ValueError as ve:
                        return json.dumps({"error": str(ve)})
                    if not result:
                        return not_found_error("Ticket", ticket_id)
                    return _safe_json({"status": "appended", "ticket": result.model_dump()})

                elif action == "update":
                    if not ticket_id:
                        return json.dumps({"error": "ticket_id is required for update"})
                    if status:
                        return json.dumps({
                            "error": "Status changes require action=\"move\", not \"update\". "
                            "Use: ticket(action=\"move\", ticket_id="
                            f"{ticket_id}, status=\"{status}\")"
                        })
                    tag_list = [t.strip() for t in tags.split(",")] if tags else None
                    data = TicketUpdate(
                        title=title,
                        description=description,
                        priority=Priority(priority) if priority else None,
                        assignee=assignee,
                        tags=tag_list,
                    )
                    try:
                        result = service.update_ticket(
                            conn, schema, ticket_id, data,
                            expected_updated_at=expected_updated_at,
                            changed_by=_actor(),
                        )
                    except service.Conflict as c:
                        return _safe_json({
                            "error": "CONFLICT",
                            "message": str(c),
                            "expected_updated_at": c.expected_updated_at,
                            "current_updated_at": c.current_updated_at,
                            "hint": "re-read with action=\"get\", merge, retry — or action=\"append\" for reports",
                        })
                    if not result:
                        return not_found_error("Ticket", ticket_id)
                    return _safe_json({"status": "updated", "ticket": result.model_dump()})

                elif action == "move":
                    if not ticket_id or not status:
                        return json.dumps(
                            {"error": "ticket_id and status are required for move"}
                        )
                    result = service.transition_ticket(
                        conn, schema, ticket_id, status, changed_by=_actor(),
                        reason=reason, revisit_by=revisit_by,
                    )
                    if not result:
                        return not_found_error("Ticket", ticket_id)

                    # Email notification for bug ticket resolutions in mcp_global
                    if (
                        notify_resolution_func
                        and schema == "mcp_global"
                        and result.type == "bug"
                        and status in ("resolved", "wont_fix", "closed")
                    ):
                        try:
                            meta = result.metadata or {}
                            reporter_email = meta.get("reporter_email")
                            if reporter_email:
                                notify_resolution_func(
                                    report={
                                        "id": result.id,
                                        "title": result.title,
                                        "user_email": reporter_email,
                                    },
                                    new_status=status,
                                )
                        except Exception:
                            pass  # Email failure should not break the transition

                    return _safe_json({"status": "transitioned", "ticket": _status_change(result)})

                elif action == "list":
                    effective_limit = min(limit, 200) if limit is not None else 20
                    effective_offset = offset if offset is not None else 0
                    filters = TicketListFilters(
                        type=TicketType(type) if type else None,
                        status=status,
                        priority=Priority(priority) if priority else None,
                        assignee=assignee,
                        tags=tags,
                        limit=effective_limit,
                        offset=effective_offset,
                        include_archived=include_archived,
                    )
                    result = service.list_tickets(conn, schema, filters, fields=fields or "card")
                    if grep and hasattr(result, "tickets"):
                        result.tickets = [
                            t for t in result.tickets
                            if fnmatch.fnmatch(t.title if hasattr(t, "title") else t.get("title", ""), grep)
                        ]
                        result.total = len(result.tickets)
                        # Recompute aggregates over the grep-filtered set so
                        # by_status / by_type match what the caller sees.
                        result.by_status = {}
                        result.by_type = {}
                        for t in result.tickets:
                            t_status = t.status if hasattr(t, "status") else t.get("status")
                            t_type = t.type if hasattr(t, "type") else t.get("type")
                            if t_status:
                                result.by_status[t_status] = result.by_status.get(t_status, 0) + 1
                            if t_type:
                                result.by_type[t_type] = result.by_type.get(t_type, 0) + 1
                    return _safe_json(result)

                elif action == "list_tags":
                    result = service.list_tags(conn, schema, include_archived=include_archived)
                    return _safe_json({"tags": result, "total": len(result)})

                elif action == "archive":
                    if ticket_id is not None or ticket_ids:
                        # A silently ignored parameter is the STOMPY-1364
                        # --deselect shape: refuse, and name the real tool.
                        return mcp_error(
                            "INVALID_PARAMS",
                            "archive is a global sweep of long-closed tickets and takes "
                            "no ticket_id/ticket_ids. To shelve a ticket, use "
                            "action='move' with status='parked' and a reason.",
                        )
                    count = service.archive_stale_tickets(conn, schema)
                    return json.dumps({
                        "status": "archived",
                        "count": count,
                        "message": f"Archived {count} stale ticket(s)",
                    })

                elif action == "close":
                    if not ticket_id:
                        return json.dumps({"error": "ticket_id is required for close"})
                    result = service.close_ticket(
                        conn, schema, ticket_id, resolution=resolution, changed_by=_actor(),
                    )
                    if not result:
                        return not_found_error("Ticket", ticket_id)
                    return _safe_json({"status": "closed", "ticket": _status_change(result)})

                elif action == "batch_move":
                    if not ticket_ids or not status:
                        return json.dumps(
                            {"error": "ticket_ids and status are required for batch_move"}
                        )
                    try:
                        parsed_ids = [int(x.strip()) for x in ticket_ids.split(",")]
                    except ValueError:
                        return json.dumps({"error": "ticket_ids must be comma-separated integers"})
                    result = service.batch_transition(
                        conn, schema, parsed_ids, status,
                        confirm=confirm, changed_by=_actor(),
                        reason=reason, revisit_by=revisit_by,
                    )
                    return _safe_json(result)

                elif action == "batch_close":
                    if not ticket_ids:
                        return json.dumps(
                            {"error": "ticket_ids is required for batch_close"}
                        )
                    try:
                        parsed_ids = [int(x.strip()) for x in ticket_ids.split(",")]
                    except ValueError:
                        return json.dumps({"error": "ticket_ids must be comma-separated integers"})
                    result = service.batch_close(
                        conn, schema, parsed_ids,
                        confirm=confirm,
                        resolution=resolution,
                    )
                    return _safe_json(result)

                else:
                    return json.dumps(
                        {
                            "error": f"Unknown action: {action}",
                            "valid_actions": [
                                "create", "get", "update", "append", "move", "list", "list_tags",
                                "close", "archive", "batch_move", "batch_close",
                            ],
                        }
                    )

        except ParkArgumentError as e:
            return recoverable_error(
                "PARK_ARGUMENT",
                str(e),
                [
                    "Pass reason='why this is deliberately not now'",
                    "revisit_by, if given, is an ISO date: YYYY-MM-DD",
                ],
            )
        except InvalidTransitionError as e:
            return json.dumps({"error": str(e), "error_type": "InvalidTransition"})
        except ValueError as e:
            return json.dumps({"error": str(e), "error_type": "ValueError"})
        except Exception as e:
            return json.dumps(
                {"error": f"Ticket operation failed: {str(e)[:200]}", "error_type": e.__class__.__name__}
            )
        finally:
            if _prefix_token is not None:
                _unbind_display(_prefix_token)

    @mcp_instance.tool()
    async def ticket_link(
        action: Annotated[
            Literal["add", "remove", "list"],
            "Operation to perform",
        ],
        ticket_id: Annotated[
            Optional[Union[int, str]], "Source ticket ID (add/list): numeric or prefixed"
        ] = None,
        target_id: Annotated[
            Optional[Union[int, str]], "Target ticket ID for ticket-to-ticket links (add): numeric or prefixed (same project)"
        ] = None,
        link_type: Annotated[
            Optional[Literal["blocks", "parent", "related", "duplicate", "implements", "references", "updates"]],
            "Relationship type (default: related)",
        ] = None,
        link_id: Annotated[Optional[int], "Link ID to remove (remove)"] = None,
        project: Annotated[Optional[str], "Project name"] = None,
        context_label: Annotated[Optional[str], "Context label for ticket↔context links (add/list)"] = None,
        context_version: Annotated[Optional[str], "Context version (default: latest)"] = "latest",
    ) -> str:
        """Link tickets to other tickets or to contexts. Pass project= on every call.

        Modes (determined by params):
          ticket↔ticket: provide target_id (link_type: blocks/parent/related/duplicate)
          ticket↔context: provide context_label (link_type: implements/references/updates/related)

        Actions:
          add    → ticket_id + target_id or context_label
          remove → link_id
          list   → ticket_id (returns all links)"""
        _lk_prefix_token = None
        try:
            _src_project, ticket_id = coerce_ticket_ref(ticket_id, project, resolve_prefix_func)
            _tgt_project, target_id = coerce_ticket_ref(target_id, project, resolve_prefix_func)
            if _src_project != project:
                if project is not None:
                    return json.dumps({
                        "error": f"Source ref belongs to project '{_src_project}' "
                        f"but project='{project}' was passed."
                    })
                project = _src_project
            if target_id is not None and _tgt_project != project:
                # minimal: cross-project links need a target_ref column on
                # ticket_links (FKs are per-schema). Explicit refusal, not a
                # silent mangle — revisit with the follow-up ticket.
                return json.dumps({
                    "error": f"Cross-project links are not supported yet "
                    f"(target resolves to '{_tgt_project}', source is "
                    f"'{project}'). Reference the ticket id in the "
                    "description for now."
                })
        except TicketRefError as e:
            return json.dumps({"error": str(e)})

        project_check = check_project_func(project)
        if project_check:
            return project_check

        try:
            project_name = get_project_func(project)
            _lk_prefix_token = _bind_display(
                project_name, get_prefix_func(project_name) if get_prefix_func else None
            )
            with get_db_func(
                project, require_write=action not in TICKET_LINK_READ_ACTIONS
            ) as conn:
                schema = _get_schema(project_name)

                if action == "add":
                    if context_label:
                        # Ticket-to-context link
                        if not ticket_id:
                            return json.dumps(
                                {"error": "ticket_id is required for context link add"}
                            )
                        # Validate link_type for context links
                        ctx_link_types = {e.value for e in ContextLinkType}
                        effective_link_type = link_type or "related"
                        if effective_link_type not in ctx_link_types:
                            effective_link_type = "related"
                        data = ContextLinkCreate(
                            context_label=context_label,
                            context_version=context_version or "latest",
                            link_type=ContextLinkType(effective_link_type),
                        )
                        result = service.add_context_link(conn, schema, ticket_id, data)
                        return _safe_json({"status": "linked", "context_link": result.model_dump()})
                    else:
                        # Ticket-to-ticket link
                        if not ticket_id or not target_id:
                            return json.dumps(
                                {"error": "ticket_id and target_id are required for ticket link add"}
                            )
                        # Validate link_type for ticket links
                        ticket_link_types = {e.value for e in LinkType}
                        effective_link_type = link_type or "related"
                        if effective_link_type not in ticket_link_types:
                            effective_link_type = "related"
                        data = TicketLinkCreate(
                            target_id=target_id,
                            link_type=LinkType(effective_link_type),
                        )
                        result = service.add_link(conn, schema, ticket_id, data)
                        return _safe_json({"status": "linked", "link": result.model_dump()})

                elif action == "remove":
                    if not link_id:
                        return json.dumps({"error": "link_id is required for remove"})
                    # Try ticket-to-ticket link first, then context link
                    removed = service.remove_link(conn, schema, link_id)
                    if not removed:
                        removed = service.remove_context_link(conn, schema, link_id)
                    if not removed:
                        return json.dumps({"error": f"Link {link_id} not found"})
                    return json.dumps({"status": "removed", "link_id": link_id})

                elif action == "list":
                    if not ticket_id:
                        return json.dumps({"error": "ticket_id is required for list"})
                    ticket_links = service.list_links(conn, schema, ticket_id)
                    context_links = service.list_context_links_for_ticket(conn, schema, ticket_id)
                    return _safe_json({
                        "ticket_id": ticket_id,
                        "ticket_links": [l.model_dump() for l in ticket_links],
                        "context_links": [l.model_dump() for l in context_links],
                    })

                else:
                    return json.dumps(
                        {"error": f"Unknown action: {action}", "valid_actions": ["add", "remove", "list"]}
                    )

        except LinkAlreadyExistsError as e:
            return json.dumps(
                {"error": str(e), "error_type": "ALREADY_LINKED"}
            )
        except Exception as e:
            return json.dumps(
                {"error": f"Link operation failed: {str(e)[:200]}", "error_type": e.__class__.__name__}
            )
        finally:
            if _lk_prefix_token is not None:
                _unbind_display(_lk_prefix_token)

    @mcp_instance.tool()
    async def ticket_board(
        view: Annotated[
            Literal["kanban", "summary", "compact"],
            "kanban=full tickets; summary=counts only; compact=id+title+priority",
        ] = "kanban",
        type: Annotated[
            Optional[Literal["task", "bug", "feature", "decision"]],
            "Filter by ticket type",
        ] = None,
        status: Annotated[Optional[str], "Filter by specific status"] = None,
        include_terminal: Annotated[bool, "Include terminal statuses (done, resolved, etc.) and parked"] = False,
        include_archived: Annotated[bool, "Include archived tickets"] = False,
        limit: Annotated[Optional[int], "Max tickets per column (default 10, 0=all)"] = None,
        project: Annotated[Optional[str], "Project name"] = None,
    ) -> str:
        """Ticket board grouped by status. Excludes terminal statuses (done, cancelled, resolved, wont_fix, shipped, rejected, decided, deferred) AND parked by default — pass include_terminal=true to show them."""
        project_check = check_project_func(project)
        if project_check:
            return project_check

        _bd_tok = None
        try:
            project_name = get_project_func(project)
            _bd_tok = _bind_display(
                project_name, get_prefix_func(project_name) if get_prefix_func else None
            )
            # One retry on Neon idle-conn drop (SSL connection closed).
            # Context manager is re-entered to obtain a fresh connection.
            last_op_error: Optional[Exception] = None
            for attempt in range(2):
                try:
                    with get_db_func(project, require_write=False) as conn:
                        schema = _get_schema(project_name)
                        result = service.board_view(
                            conn, schema,
                            type_filter=type,
                            view=view,
                            status_filter=status,
                            include_terminal=include_terminal,
                            include_archived=include_archived,
                            limit=limit,
                        )
                        return _safe_json(result)
                except _OperationalError as op_err:
                    last_op_error = op_err
                    if attempt == 0:
                        _time.sleep(0.1)
                        continue
                    raise
            # Defensive: should not reach here (loop either returns or raises)
            raise last_op_error or RuntimeError("board_view retry loop fell through")

        except Exception as e:
            return json.dumps(
                {"error": f"Board view failed: {str(e)[:200]}", "error_type": e.__class__.__name__}
            )
        finally:
            if _bd_tok is not None:
                _unbind_display(_bd_tok)

    @mcp_instance.tool()
    async def ticket_search(
        query: Annotated[str, "Search query string"],
        type: Annotated[
            Optional[Literal["task", "bug", "feature", "decision"]],
            "Filter by ticket type",
        ] = None,
        status: Annotated[Optional[str], "Filter by status"] = None,
        limit: Annotated[int, "Max results"] = 20,
        include_archived: Annotated[bool, "Include archived tickets"] = False,
        regex: Annotated[str, "Post-filter the RESULT SET by Python regex (case-insensitive) matched against each hit's title and description body — it narrows which tickets come back, not what each row contains. E.g. 'conflict.*false', 'MUST.*deploy'"] = "",
        project: Annotated[Optional[str], "Project name"] = None,
        fields: Annotated[
            Optional[Literal["card", "full"]],
            "'card' (default) = id/title/type/status/priority/tags/description_preview per hit; 'full' = whole records incl. description",
        ] = None,
    ) -> str:
        """Full-text search (BM25) over tickets. Returns CARDS (no description body, a bounded description_preview) — ticket(action='get') has the full record; fields='full' for bodies. Excludes archived by default. regex narrows the result set by title/body match."""
        project_check = check_project_func(project)
        if project_check:
            return project_check

        # Validate regex early
        compiled_regex = None
        if regex:
            import re as _re
            if len(regex) > 500:
                return json.dumps({"error": "Regex pattern too long (max 500 chars)"})
            try:
                compiled_regex = _re.compile(regex, _re.IGNORECASE)
            except _re.error as exc:
                return json.dumps({"error": f"Invalid regex pattern: {exc}"})

        _sr_tok = None
        try:
            project_name = get_project_func(project)
            _sr_tok = _bind_display(
                project_name, get_prefix_func(project_name) if get_prefix_func else None
            )
            fetch_limit = limit * 3 if compiled_regex else limit
            with get_db_func(project, require_write=False) as conn:
                schema = _get_schema(project_name)
                want = fields or "card"
                # regex matches against the BODY, so fetch full rows to
                # filter on and project to cards afterwards (STOMPY-1923).
                result = service.search_tickets(
                    conn, schema, query,
                    type_filter=type,
                    status_filter=status,
                    limit=fetch_limit,
                    include_archived=include_archived,
                    fields="full" if compiled_regex else want,
                )
                if compiled_regex and result.tickets:
                    result.tickets = [
                        t for t in result.tickets
                        if compiled_regex.search(t.title or "") or compiled_regex.search(t.description or "")
                    ][:limit]
                    result.total = len(result.tickets)
                    if want != "full":
                        result.tickets = [TicketService.to_card(t) for t in result.tickets]
                return _safe_json(result)

        except Exception as e:
            return json.dumps(
                {"error": f"Search failed: {str(e)[:200]}", "error_type": e.__class__.__name__}
            )
        finally:
            if _sr_tok is not None:
                _unbind_display(_sr_tok)
