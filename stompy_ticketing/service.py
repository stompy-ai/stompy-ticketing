"""TicketService - business logic and state machine for stompy-ticketing.

Follows the ContextService pattern from dementia-production:
- Takes a DB connection/adapter as parameter (DI)
- Sync methods with psycopg2 RealDictCursor
- TEXT for JSON strings (tags, metadata), DOUBLE PRECISION timestamps
"""

import json
import re
import time
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from psycopg2 import IntegrityError, sql
from stompy_ticketing.archival import ArchiveMixin
from stompy_ticketing.board_view import BoardMixin, context_links_present
from stompy_ticketing.duplicates import content_hash as duplicate_content_hash
from stompy_ticketing.stale_archive import run_if_due, sweep_if_due

from stompy_ticketing.models import (
    BatchItemResult,
    BatchOperationResult,
    ContextLinkCreate,
    LinkType,
    ContextLinkResponse,
    Priority,
    SearchResult,
    TicketCreate,
    TicketHistoryEntry,
    TicketLinkCreate,
    TicketLinkResponse,
    TicketListFilters,
    TicketListResponse,
    TicketResponse,
    TicketType,
    TicketUpdate,
)


# =========================================================================== #
# State Machine                                                               #
# =========================================================================== #


class InvalidTransitionError(Exception):
    """Raised when a ticket transition violates the state machine."""

    pass


_SYMMETRIC_LINK_TYPES = frozenset({LinkType.related})


class LinkAlreadyExistsError(ValueError):
    """Raised when add_link/add_context_link hits a duplicate UNIQUE constraint.

    Subclasses ValueError so existing callers that catch ValueError keep
    working; new callers can catch this specifically to return a structured
    `ALREADY_LINKED` error code instead of a raw IntegrityError.
    `link_id` is the existing link's id when known (STOMPY-2880).
    """

    def __init__(self, message: str, link_id: Optional[int] = None):
        super().__init__(message)
        self.link_id = link_id


class LinkRefusedError(ValueError):
    """add_link refused a link that can never be valid (STOMPY-2871).

    `code` is SELF_LINK or LINK_CYCLE. Subclasses ValueError so the host
    books it as a client refusal, not a server fault.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# PARKED (STOMPY-1746): "deliberately not now". Reachable from every
# pre-work status of every type and reopenable to those same statuses in
# ONE step. Deliberately NOT terminal — closed_at stays NULL and the stale
# sweep never archives it — but board views hide it with the terminals so
# the queue stays a queue. Entering it requires a reason (transition_ticket).
PARKED = "parked"


class ParkArgumentError(InvalidTransitionError):
    """Parking was asked for without a reason, or with an unparseable
    revisit_by. Subclasses InvalidTransitionError so the REST door's
    existing 422 mapping applies; the MCP door maps it to PARK_ARGUMENT."""

    pass


# Each type maps to: initial status, terminal statuses, and allowed transitions
STATE_MACHINES: Dict[str, Dict[str, Any]] = {
    "task": {
        "initial": "backlog",
        "terminal": ["done", "cancelled"],
        "transitions": {
            "backlog": ["in_progress", "done", "cancelled", PARKED],
            "in_progress": ["done", "cancelled", "backlog"],  # backlog: a stalled start steps back
            "done": ["backlog"],
            "cancelled": ["backlog"],
            PARKED: ["backlog"],
        },
    },
    "bug": {
        "initial": "triage",
        "terminal": ["resolved", "wont_fix"],
        "transitions": {
            "triage": ["confirmed", "wont_fix", PARKED],
            "confirmed": ["in_progress", PARKED],
            "in_progress": ["resolved", "wont_fix", "confirmed"],
            "resolved": ["triage"],
            "wont_fix": ["triage"],
            PARKED: ["triage", "confirmed"],
        },
    },
    "feature": {
        "initial": "proposed",
        "terminal": ["shipped", "rejected"],
        "transitions": {
            "proposed": ["approved", "rejected", PARKED],
            "approved": ["in_progress", PARKED],
            "in_progress": ["shipped", "rejected", "approved"],
            "shipped": ["proposed"],
            "rejected": ["proposed"],
            PARKED: ["proposed", "approved"],
        },
    },
    "decision": {
        "initial": "open",
        "terminal": ["decided", "deferred"],
        "transitions": {
            "open": ["decided", "deferred", PARKED],
            "decided": ["open"],
            "deferred": ["open"],  # Deferred decisions can be reopened
            PARKED: ["open"],
        },
    },
}


# Preferred (positive) terminal status per type — used by close_ticket/batch_close (#163)
POSITIVE_TERMINALS: Dict[str, str] = {
    "task": "done",
    "bug": "resolved",
    "feature": "shipped",
    "decision": "decided",
}


def get_initial_status(ticket_type: str) -> str:
    """Get the initial status for a ticket type."""
    if ticket_type not in STATE_MACHINES:
        raise ValueError(f"Unknown ticket type: {ticket_type}")
    return STATE_MACHINES[ticket_type]["initial"]


def get_terminal_statuses(ticket_type: str) -> List[str]:
    """Get terminal (closed) statuses for a ticket type."""
    if ticket_type not in STATE_MACHINES:
        raise ValueError(f"Unknown ticket type: {ticket_type}")
    return STATE_MACHINES[ticket_type]["terminal"]


def get_all_statuses(ticket_type: str) -> List[str]:
    """Get all valid statuses for a ticket type."""
    if ticket_type not in STATE_MACHINES:
        raise ValueError(f"Unknown ticket type: {ticket_type}")
    return list(STATE_MACHINES[ticket_type]["transitions"].keys())


def get_all_terminal_statuses() -> List[str]:
    """Get all terminal statuses across all ticket types (deduplicated)."""
    terminals = set()
    for sm in STATE_MACHINES.values():
        terminals.update(sm["terminal"])
    return sorted(terminals)


def get_hidden_statuses(ticket_type: Optional[str] = None) -> List[str]:
    """Statuses a default board/queue view hides: the terminals plus PARKED.

    Per type when given (and known), else across all types. Parked is not
    terminal, but a parked ticket is not queue either (STOMPY-1746)."""
    if ticket_type and ticket_type in STATE_MACHINES:
        hidden = set(get_terminal_statuses(ticket_type))
    else:
        hidden = set(get_all_terminal_statuses())
    hidden.add(PARKED)
    return sorted(hidden)


PARK_REASON_MAX = 2000


def _validate_park_args(
    target_status: str, reason: Optional[str], revisit_by: Optional[str]
) -> None:
    """Refuse a park without a reason, a bad revisit_by, or park-only args
    on a non-park move (a silently dropped argument is the STOMPY-1364
    shape this feature exists to end)."""
    if target_status != PARKED:
        if reason is not None or revisit_by is not None:
            raise ParkArgumentError(
                "reason/revisit_by only apply to status='parked'; "
                f"drop them for a move to '{target_status}'"
            )
        return
    if not isinstance(reason, str) or not reason.strip():
        raise ParkArgumentError(
            "Parking requires a reason: pass reason='why this is not now'"
        )
    if len(reason) > PARK_REASON_MAX:
        raise ParkArgumentError(
            f"reason is {len(reason)} chars; max {PARK_REASON_MAX} — put the essay on the ticket"
        )
    if revisit_by is not None:
        try:
            date.fromisoformat(revisit_by)
        except (ValueError, TypeError):
            raise ParkArgumentError(
                f"revisit_by must be an ISO date (YYYY-MM-DD), got {revisit_by!r}"
            )


def _park_metadata(
    current_metadata: Optional[str],
    current_status: str,
    target_status: str,
    reason: Optional[str],
    revisit_by: Optional[str],
    now: float,
) -> Optional[str]:
    """Serialized metadata to write for a park/reopen, or None to leave the
    column untouched. The `metadata.parked` contract (the web board reads
    it): {reason, revisit_by (ISO date or null), parked_at (epoch),
    from_status}; present only while status == parked."""
    _validate_park_args(target_status, reason, revisit_by)
    if target_status != PARKED and current_status != PARKED:
        return None
    meta: Dict[str, Any] = {}
    if current_metadata:
        try:
            meta = json.loads(current_metadata) or {}
        except (json.JSONDecodeError, TypeError):
            # Unreadable already; keep the bytes rather than destroy evidence.
            meta = {"_unparseable_metadata": str(current_metadata)}
    if target_status == PARKED:
        meta["parked"] = {
            "reason": reason.strip(),
            "revisit_by": revisit_by,
            "parked_at": now,
            "from_status": current_status,
        }
    else:
        meta.pop("parked", None)
    return json.dumps(meta)


def validate_transition(
    ticket_type: str,
    current_status: str,
    target_status: str,
    raise_on_invalid: bool = True,
) -> bool:
    """Validate a status transition against the state machine.

    Args:
        ticket_type: The ticket type (task, bug, feature, decision).
        current_status: Current status of the ticket.
        target_status: Desired target status.
        raise_on_invalid: If True, raise InvalidTransitionError; otherwise return False.

    Returns:
        True if transition is valid.

    Raises:
        InvalidTransitionError: If transition is invalid and raise_on_invalid=True.
        ValueError: If ticket_type is unknown.
    """
    if ticket_type not in STATE_MACHINES:
        raise ValueError(f"Unknown ticket type: {ticket_type}")

    sm = STATE_MACHINES[ticket_type]
    transitions = sm["transitions"]

    if current_status not in transitions:
        if raise_on_invalid:
            raise InvalidTransitionError(
                f"'{current_status}' is not a valid status for type '{ticket_type}'. "
                f"Valid statuses: {list(transitions.keys())}"
            )
        return False

    allowed = transitions[current_status]
    if target_status not in allowed:
        if raise_on_invalid:
            # STOMPY-1895: the error teaches the PATH, not just the next hop —
            # otherwise an agent learns the graph one refusal per state.
            path = find_transition_path(ticket_type, current_status, target_status)
            if path:
                hint = f" Path: {' → '.join(path)}."
                if target_status in sm["terminal"]:
                    hint += (
                        f" action='close' with resolution='{target_status}' walks it in one call."
                    )
            else:
                hint = f" '{target_status}' is not reachable from '{current_status}'."
            raise InvalidTransitionError(
                f"Cannot transition {ticket_type} from '{current_status}' to "
                f"'{target_status}'. Allowed next: {allowed}.{hint}"
            )
        return False

    return True


def find_transition_path(ticket_type: str, current_status: str, target_status: str) -> Optional[List[str]]:
    """Shortest legal path from current_status to target_status (BFS over the
    type's transitions), excluding the start; None when unreachable."""
    from collections import deque

    transitions = STATE_MACHINES.get(ticket_type, {}).get("transitions", {})
    if current_status == target_status:
        return None
    queue: deque = deque([(current_status, [])])
    visited = {current_status}
    while queue:
        status, path = queue.popleft()
        for nxt in transitions.get(status, []):
            if nxt in visited:
                continue
            new_path = path + [nxt]
            if nxt == target_status:
                return new_path
            visited.add(nxt)
            queue.append((nxt, new_path))
    return None


# =========================================================================== #
# Database Protocol                                                           #
# =========================================================================== #


class DBConnection(Protocol):
    """Protocol for database connections used by TicketService.

    This matches the psycopg2 connection interface.
    The caller is responsible for connection lifecycle.
    """

    def cursor(self, **kwargs) -> Any: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


# =========================================================================== #
# TicketService                                                               #
# =========================================================================== #


class TicketService(ArchiveMixin, BoardMixin):
    """Business logic for ticket operations.

    Takes a database connection and schema as parameters (dependency injection).
    The service does NOT manage connection lifecycle - the caller does.
    """

    def create_ticket(
        self,
        conn: DBConnection,
        schema: str,
        data: TicketCreate,
        changed_by: Optional[str] = None,
    ) -> TicketResponse:
        """Create a new ticket.

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name (project).
            data: Ticket creation data.
            changed_by: Who created the ticket.

        Returns:
            Created ticket response.
        """
        ticket_type = data.type.value
        initial_status = get_initial_status(ticket_type)
        now = time.time()

        tags_json = json.dumps(data.tags) if data.tags else None
        metadata_json = json.dumps(data.metadata) if data.metadata else None

        # Read by duplicates.find_possible_duplicates as its exact-match path (2455)
        content_hash = duplicate_content_hash(data.title, data.description)

        cur = conn.cursor()
        try:
            cur.execute(
                sql.SQL("""
                INSERT INTO {}.tickets
                    (title, description, type, status, priority, assignee,
                     tags, metadata, session_id, content_hash, created_at, updated_at,
                     created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """).format(sql.Identifier(schema)),
                (
                    data.title,
                    data.description,
                    ticket_type,
                    initial_status,
                    data.priority.value,
                    data.assignee,
                    tags_json,
                    metadata_json,
                    data.session_id,
                    content_hash,
                    now,
                    now,
                    changed_by,  # the filer (STOMPY-1594)
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return self._row_to_response(row)
        except Exception:
            conn.rollback()
            raise

    def get_ticket(
        self,
        conn: DBConnection,
        schema: str,
        ticket_id: int,
        include_history: bool = True,
        include_links: bool = True,
    ) -> Optional[TicketResponse]:
        """Get a ticket by ID with optional history and links.

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            ticket_id: Ticket ID.
            include_history: Include change history.
            include_links: Include ticket links.

        Returns:
            Ticket response or None if not found.
        """
        cur = conn.cursor()
        cur.execute(
            sql.SQL("SELECT * FROM {}.tickets WHERE id = %s").format(
                sql.Identifier(schema)
            ),
            (ticket_id,),
        )
        row = cur.fetchone()
        if not row:
            return None

        response = self._row_to_response(row)

        if include_history:
            response.history = self._fetch_history(cur, schema, ticket_id)

        if include_links:
            response.links = self._get_links_for_ticket(cur, schema, ticket_id)
            # A schema predating migration 32 has no context links; a failed
            # read here would abort the caller's transaction (STOMPY-3296).
            if context_links_present(conn, cur, schema):
                response.context_links = self.list_context_links_for_ticket(
                    conn, schema, ticket_id
                )

        return response

    class Conflict(Exception):
        """update_ticket's optimistic guard fired: the ticket changed since the
        caller read it (STOMPY-1579 — a wholesale description replace silently
        clobbered a parallel agent's re-scope). Carries both timestamps so the
        caller can re-read, merge, and retry — or switch to append_description."""

        def __init__(self, expected_updated_at: float, current_updated_at: float):
            self.expected_updated_at = expected_updated_at
            self.current_updated_at = current_updated_at
            super().__init__(
                f"ticket changed since your read (updated_at {current_updated_at}, "
                f"you expected {expected_updated_at}) — re-read and retry, or use append"
            )

    class DescriptionWouldReplace(Exception):
        """update_ticket refused to REPLACE a non-empty description with text
        that does not contain it (STOMPY-2394): twice an agent meaning to add a
        note sent update(description=<the note>) and wiped the ticket body.
        Nothing was written. append_description adds; confirm=True replaces."""

        def __init__(self, stored_length: int):
            self.stored_length = stored_length
            super().__init__(
                f"update would replace the stored description ({stored_length} chars) "
                "with text that does not contain it, so nothing was written. Use "
                'action="append" to add to it, or pass confirm=true to replace it.'
            )

    MAX_APPEND_CHARS = 20_000

    def append_description(
        self,
        conn: DBConnection,
        schema: str,
        ticket_id: int,
        text: str,
        changed_by: Optional[str] = None,
        separator: str = "\n\n",
    ) -> Optional[TicketResponse]:
        """Append to the description ATOMICALLY in SQL (STOMPY-1579).

        No read-modify-write: concurrent appends from parallel agents both
        land, in commit order (the row lock serializes them). updated_at is
        bumped IN THE SAME STATEMENT so a stale guarded update afterwards
        conflicts instead of erasing the append. History records the
        appended text only. Empty text and text over MAX_APPEND_CHARS are
        rejected (a looping agent must not grow a row without bound).
        """
        if not text or not text.strip():
            raise ValueError("append text must be non-empty")
        if len(text) > self.MAX_APPEND_CHARS:
            raise ValueError(f"append text exceeds {self.MAX_APPEND_CHARS} chars")
        cur = conn.cursor()
        try:
            now = time.time()
            cur.execute(
                sql.SQL(
                    """
                UPDATE {}.tickets
                SET description = CASE
                        WHEN description IS NULL OR description = '' THEN %s
                        ELSE description || %s || %s
                    END,
                    updated_at = %s
                WHERE id = %s
                RETURNING *
                """
                ).format(sql.Identifier(schema)),
                (text, separator, text, now, ticket_id),
            )
            updated_row = cur.fetchone()
            if not updated_row:
                conn.rollback()
                return None
            cur.execute(
                sql.SQL(
                    """
                INSERT INTO {}.ticket_history
                    (ticket_id, field_name, old_value, new_value, changed_by, changed_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """
                ).format(sql.Identifier(schema)),
                (ticket_id, "description_appended", None, text, changed_by, now),
            )
            conn.commit()
            return self._row_to_response(updated_row)
        except Exception:
            conn.rollback()
            raise

    def update_ticket(
        self,
        conn: DBConnection,
        schema: str,
        ticket_id: int,
        data: TicketUpdate,
        changed_by: Optional[str] = None,
        expected_updated_at: Optional[float] = None,
        confirm: bool = False,
    ) -> Optional[TicketResponse]:
        """Update ticket fields (not status - use transition_ticket for that).

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            ticket_id: Ticket ID.
            data: Fields to update.
            changed_by: Who made the change.
            expected_updated_at: optimistic guard (STOMPY-1579) — refuse with
                Conflict when the stored updated_at differs (>1ms) from this.
            confirm: allow a description REPLACE (STOMPY-2394). Without it, a
                description that does not contain the stored non-empty one
                raises DescriptionWouldReplace and nothing is written.

        Returns:
            Updated ticket or None if not found.
        """
        if expected_updated_at is None:
            # STOMPY-2961: the REST body carries the guard on the model
            expected_updated_at = data.expected_updated_at
        confirm = confirm or data.confirm  # STOMPY-2394: same split as the guard above
        cur = conn.cursor()
        try:
            # Get current ticket
            cur.execute(
                sql.SQL("SELECT * FROM {}.tickets WHERE id = %s").format(
                    sql.Identifier(schema)
                ),
                (ticket_id,),
            )
            current = cur.fetchone()
            if not current:
                return None

            # The guard itself lives in the UPDATE's WHERE clause (single-statement
            # compare-and-swap) — a Python-side compare here would be check-then-act
            # and lose the race it exists to close (Kimi #26). This early read only
            # feeds history diffs.

            now = time.time()
            updates = {}
            history_entries = []

            # Check each updatable field
            if data.title is not None and data.title != current["title"]:
                updates["title"] = data.title
                history_entries.append(("title", current["title"], data.title))

            if data.description is not None and data.description != current.get("description"):
                stored = (current.get("description") or "").strip()
                if stored and stored not in data.description and not confirm:
                    raise TicketService.DescriptionWouldReplace(len(current["description"]))
                updates["description"] = data.description
                history_entries.append(("description", current.get("description"), data.description))

            if data.type is not None and data.type.value != current["type"]:
                updates["type"] = data.type.value
                history_entries.append(("type", current["type"], data.type.value))

            if data.priority is not None and data.priority.value != current["priority"]:
                updates["priority"] = data.priority.value
                history_entries.append(("priority", current["priority"], data.priority.value))

            if data.assignee is not None and data.assignee != current.get("assignee"):
                updates["assignee"] = data.assignee
                history_entries.append(("assignee", current.get("assignee"), data.assignee))

            if data.tags is not None:
                new_tags = json.dumps(data.tags)
                if new_tags != current.get("tags"):
                    updates["tags"] = new_tags
                    history_entries.append(("tags", current.get("tags"), new_tags))

            if data.metadata is not None:
                new_metadata = json.dumps(data.metadata)
                if new_metadata != current.get("metadata"):
                    updates["metadata"] = new_metadata
                    history_entries.append(("metadata", current.get("metadata"), new_metadata))

            if not updates:
                return self._row_to_response(current)

            # Build UPDATE query. Column names come from the fixed `updates`
            # keys assigned above (a server-side whitelist), never from input.
            set_clauses = [f"{col} = %s" for col in updates]
            set_clauses.append("updated_at = %s")
            values = list(updates.values()) + [now, ticket_id]
            where = "WHERE id = %s"
            if expected_updated_at is not None:
                # atomic compare-and-swap: the row must still be the one the
                # caller read — exact equality, enforced by the database
                where += " AND updated_at = %s"
                values.append(expected_updated_at)
            # STOMPY-3275: a description write is a compare-and-swap on the text the
            # 2394 guard judged, so an append landing between the read and this write
            # conflicts instead of being erased (confirm or not).
            guard_description = "description" in updates
            if guard_description:
                where += " AND description IS NOT DISTINCT FROM %s"
                values.append(current.get("description"))

            cur.execute(
                sql.SQL("""
                UPDATE {}.tickets
                SET {}
                {}
                RETURNING *
                """).format(
                    sql.Identifier(schema),
                    sql.SQL(', ').join(sql.SQL(c) for c in set_clauses),
                    sql.SQL(where),
                ),
                values,
            )
            updated_row = cur.fetchone()
            if updated_row is None and (expected_updated_at is not None or guard_description):
                # zero rows under the guard: gone, or moved — tell them which
                cur.execute(
                    sql.SQL("SELECT updated_at FROM {}.tickets WHERE id = %s").format(
                        sql.Identifier(schema)
                    ),
                    (ticket_id,),
                )
                row = cur.fetchone()
                conn.rollback()
                if row is None:
                    return None
                raise TicketService.Conflict(
                    expected_updated_at if expected_updated_at is not None else current["updated_at"],
                    row.get("updated_at") or 0.0,
                )

            # Record history
            for field_name, old_val, new_val in history_entries:
                cur.execute(
                    sql.SQL("""
                    INSERT INTO {}.ticket_history
                        (ticket_id, field_name, old_value, new_value, changed_by, changed_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """).format(sql.Identifier(schema)),
                    (ticket_id, field_name, old_val, new_val, changed_by, now),
                )

            conn.commit()
            response = self._row_to_response(updated_row)
            response.history = self._fetch_history(cur, schema, ticket_id)
            return response
        except Exception:
            conn.rollback()
            raise

    def transition_ticket(
        self,
        conn: DBConnection,
        schema: str,
        ticket_id: int,
        target_status: str,
        changed_by: Optional[str] = None,
        reason: Optional[str] = None,
        revisit_by: Optional[str] = None,
    ) -> Optional[TicketResponse]:
        """Transition a ticket to a new status via the state machine.

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            ticket_id: Ticket ID.
            target_status: Target status.
            changed_by: Who made the change.
            reason: REQUIRED when target_status is PARKED; ignored otherwise.
            revisit_by: Optional ISO date (YYYY-MM-DD) for a park.

        Returns:
            Updated ticket or None if not found.

        Raises:
            InvalidTransitionError: If transition is not allowed.
            ParkArgumentError: Park without a reason / bad revisit_by.
        """
        cur = conn.cursor()
        try:
            cur.execute(
                sql.SQL("SELECT * FROM {}.tickets WHERE id = %s").format(
                    sql.Identifier(schema)
                ),
                (ticket_id,),
            )
            current = cur.fetchone()
            if not current:
                return None

            ticket_type = current["type"]
            current_status = current["status"]

            # Validate transition
            validate_transition(ticket_type, current_status, target_status)

            now = time.time()
            closed_at = now if target_status in get_terminal_statuses(ticket_type) else None
            # None = leave metadata alone; a string = park/reopen rewrote it.
            metadata_text = _park_metadata(
                current.get("metadata"), current_status, target_status, reason, revisit_by, now
            )

            set_sql = "status = %s, updated_at = %s, closed_at = %s"
            params: List[Any] = [target_status, now, closed_at]
            if metadata_text is not None:
                set_sql += ", metadata = %s"
                params.append(metadata_text)
            cur.execute(
                sql.SQL("""
                UPDATE {}.tickets
                SET {}
                WHERE id = %s
                RETURNING *
                """).format(sql.Identifier(schema), sql.SQL(set_sql)),
                tuple(params) + (ticket_id,),
            )
            updated_row = cur.fetchone()

            # Record history
            history_sql = sql.SQL("""
                INSERT INTO {}.ticket_history
                    (ticket_id, field_name, old_value, new_value, changed_by, changed_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """).format(sql.Identifier(schema))
            cur.execute(
                history_sql,
                (ticket_id, "status", current_status, target_status, changed_by, now),
            )
            if target_status == PARKED:
                # The reason outlives the park: metadata.parked is cleared on
                # reopen, history keeps why it was shelved.
                cur.execute(
                    history_sql,
                    (ticket_id, "parked_reason", None, reason.strip(), changed_by, now),
                )

            conn.commit()
            response = self._row_to_response(updated_row)
            response.history = self._fetch_history(cur, schema, ticket_id)
            return response
        except Exception:
            conn.rollback()
            raise

    def close_ticket(
        self,
        conn: DBConnection,
        schema: str,
        ticket_id: int,
        changed_by: Optional[str] = None,
        resolution: Optional[str] = None,
    ) -> Optional[TicketResponse]:
        """Close a ticket by walking it to a terminal status.

        Prefers the positive terminal (resolved, done, shipped, decided)
        unless a specific resolution is given.

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            ticket_id: Ticket ID.
            changed_by: Who closed it.
            resolution: Specific terminal status to target (e.g. "wont_fix").
                If None, uses the positive default for the ticket type.

        Returns:
            Updated ticket or None if not found.
        """
        cur = conn.cursor()
        cur.execute(
            sql.SQL("SELECT type, status FROM {}.tickets WHERE id = %s").format(
                sql.Identifier(schema)
            ),
            (ticket_id,),
        )
        current = cur.fetchone()
        if not current:
            return None

        ticket_type = current["type"]
        current_status = current["status"]

        # Already closed?
        terminals = get_terminal_statuses(ticket_type)
        if current_status in terminals:
            return self.get_ticket(conn, schema, ticket_id, include_history=False, include_links=False)

        # Validate resolution if provided
        if resolution and resolution not in terminals:
            raise InvalidTransitionError(
                f"'{resolution}' is not a terminal status for {ticket_type}. "
                f"Valid terminals: {terminals}"
            )

        # Find path to terminal (prefers positive terminal by default)
        path = self._find_close_path(ticket_type, current_status, resolution)
        if not path:
            sm = STATE_MACHINES[ticket_type]
            allowed = sm["transitions"].get(current_status, [])
            raise InvalidTransitionError(
                f"Cannot close {ticket_type} from '{current_status}'. "
                f"No path to terminal status. "
                f"Allowed transitions: {allowed}"
            )

        # Walk the path
        result = None
        for step_status in path:
            result = self.transition_ticket(conn, schema, ticket_id, step_status, changed_by)
        return result

    def list_tickets(
        self,
        conn: DBConnection,
        schema: str,
        filters: Optional[TicketListFilters] = None,
        fields: str = "card",
    ) -> TicketListResponse:
        """List tickets with optional filters.

        Rows are CARDS unless fields="full" (STOMPY-1923): list is the
        orientation call and must be the cheapest one; `get` has the body.

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            filters: Filter/pagination options.
            fields: "card" (default) or "full".

        Returns:
            List of tickets with counts.
        """
        if filters is None:
            filters = TicketListFilters()

        sweep_if_due(self, conn, schema)  # throttled, never fails the read (3288)

        cur = conn.cursor()
        where_clauses: List[str] = []
        params: List[Any] = []

        if not filters.include_archived:
            where_clauses.append("archived_at IS NULL")

        if filters.type:
            where_clauses.append("type = %s")
            params.append(filters.type.value)

        if filters.status:
            where_clauses.append("status = %s")
            params.append(filters.status)

        if filters.priority:
            where_clauses.append("priority = %s")
            params.append(filters.priority.value)

        if filters.assignee:
            where_clauses.append("assignee = %s")
            params.append(filters.assignee)

        if filters.tags:
            tag_list = [t.strip() for t in filters.tags.split(",") if t.strip()]
            if tag_list:
                tag_clauses = " OR ".join("tags LIKE %s" for _ in tag_list)
                where_clauses.append(f"({tag_clauses})")
                # Escape LIKE metacharacters in tag values before wrapping with %
                for t in tag_list:
                    escaped = t.replace("%", r"\%").replace("_", r"\_")
                    params.append(f"%{escaped}%")

        if filters.search:
            tsquery_param = self._build_or_tsquery_param(filters.search)
            where_clauses.append(
                "content_tsvector @@ to_tsquery('english', %s)"
            )
            params.append(tsquery_param)

        where_sql = ""
        if where_clauses:
            where_sql = "WHERE " + " AND ".join(where_clauses)

        # Get filtered tickets
        cur.execute(
            sql.SQL("""
            SELECT * FROM {}.tickets
            {}
            ORDER BY
                CASE priority
                    WHEN 'urgent' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2
                    WHEN 'low' THEN 3
                    ELSE 4
                END,
                updated_at DESC
            LIMIT %s OFFSET %s
            """).format(sql.Identifier(schema), sql.SQL(where_sql)),
            params + [filters.limit, filters.offset],
        )
        rows = cur.fetchall()

        # Get total count
        cur.execute(
            sql.SQL("SELECT COUNT(*) as count FROM {}.tickets {}").format(
                sql.Identifier(schema), sql.SQL(where_sql)
            ),
            params,
        )
        total = cur.fetchone()["count"]

        # Get counts by status
        cur.execute(
            sql.SQL(
                "SELECT status, COUNT(*) as count FROM {}.tickets {} GROUP BY status"
            ).format(sql.Identifier(schema), sql.SQL(where_sql)),
            params,
        )
        by_status = {r["status"]: r["count"] for r in cur.fetchall()}

        # Get counts by type
        cur.execute(
            sql.SQL(
                "SELECT type, COUNT(*) as count FROM {}.tickets {} GROUP BY type"
            ).format(sql.Identifier(schema), sql.SQL(where_sql)),
            params,
        )
        by_type = {r["type"]: r["count"] for r in cur.fetchall()}

        tickets = [self._row_to_response(r) for r in rows]
        if fields != "full":
            tickets = [self.to_card(t) for t in tickets]
        return TicketListResponse(
            tickets=tickets,
            total=total,
            limit=filters.limit,
            offset=filters.offset,
            has_more=(filters.offset + filters.limit) < total,
            by_status=by_status,
            by_type=by_type,
        )

    @staticmethod
    def _build_or_tsquery_param(query: str) -> str:
        """Build an OR-joined tsquery parameter string from a free-text query.

        Splits the query into words, strips whitespace, removes empty tokens,
        and joins with ' | ' for OR semantics in to_tsquery('english', ...).

        This enables partial matching: "dogfood test verification" becomes
        "dogfood | test | verification", so documents matching ANY term are
        returned (ranked by how many terms match via ts_rank).

        Stemming is handled by to_tsquery('english', ...) at query time,
        e.g. "verification" -> stem "verifi" matches stored "verify" -> "verifi".

        Args:
            query: Raw search query string.

        Returns:
            OR-joined term string suitable for to_tsquery('english', ...).
        """
        # Strip wildcard characters and tsquery-breaking chars (hyphens, colons,
        # parens, etc.), then split on whitespace and filter empty tokens.
        cleaned = query.replace("*", "")
        cleaned = re.sub(r"[^\w\s]", " ", cleaned)
        terms = [t.strip() for t in cleaned.split() if t.strip()]
        if not terms:
            return ""
        return " | ".join(terms)

    def list_tags(
        self,
        conn,
        schema: str,
        include_archived: bool = False,
    ) -> List[Dict[str, Any]]:
        """List all unique tags with usage counts across tickets.

        Uses jsonb_array_elements_text to unnest the JSON array stored in the
        TEXT tags column. Tickets with NULL tags are naturally excluded by the
        lateral join.
        """
        cur = conn.cursor()
        where = sql.SQL("WHERE archived_at IS NULL") if not include_archived else sql.SQL("")
        cur.execute(
            sql.SQL(
                "SELECT tag, COUNT(*) as count"
                " FROM {}.tickets, jsonb_array_elements_text(tags::jsonb) AS tag"
                " {} GROUP BY tag ORDER BY count DESC, tag ASC"
            ).format(sql.Identifier(schema), where),
        )
        return [{"tag": r["tag"], "count": r["count"]} for r in cur.fetchall()]

    #: Rows _backfill_null_tsvectors fixes per call.
    TSVECTOR_BACKFILL_LIMIT = 200

    def _backfill_null_tsvectors(
        self, conn: DBConnection, schema: str, limit: int = TSVECTOR_BACKFILL_LIMIT
    ) -> int:
        """Backfill NULL content_tsvector rows from title+description.

        Tickets created before the tsvector trigger migration have NULL
        content_tsvector, making them invisible to BM25 search.  This lazily
        fixes up to *limit* rows per call (same pattern as
        hybrid_search_service._backfill_null_tsvectors).

        Returns:
            Number of rows backfilled.
        """
        cur = conn.cursor()
        try:
            cur.execute(
                sql.SQL("""
                    UPDATE {schema}.tickets
                    SET content_tsvector = to_tsvector('english',
                        coalesce(title, '') || ' ' || coalesce(description, ''))
                    WHERE id IN (
                        SELECT id FROM {schema}.tickets
                        WHERE content_tsvector IS NULL
                          AND (title IS NOT NULL OR description IS NOT NULL)
                        LIMIT %s
                    )
                """).format(schema=sql.Identifier(schema)),
                (limit,),
            )
        except Exception:
            # A failed UPDATE aborts the transaction; the search reads next.
            conn.rollback()
            raise
        count = cur.rowcount or 0
        if count > 0:
            conn.commit()
        return count

    def search_tickets(
        self,
        conn: DBConnection,
        schema: str,
        query: str,
        type_filter: Optional[str] = None,
        status_filter: Optional[str] = None,
        limit: int = 20,
        include_archived: bool = False,
        fields: str = "card",
    ) -> SearchResult:
        """Full-text search tickets using tsvector with OR-based ranking.

        Rows are CARDS unless fields="full" (STOMPY-1923): no description
        body, a bounded description_preview instead.

        Uses OR logic between query terms so that partial matches are returned,
        ranked by ts_rank (documents matching more terms rank higher).
        Stemming is applied via the 'english' text search configuration.

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            query: Search query string.
            type_filter: Filter by type.
            status_filter: Filter by status.
            limit: Max results.
            include_archived: Include archived tickets (default False).

        Returns:
            Search results ranked by relevance.
        """
        sweep_if_due(self, conn, schema)  # throttled, never fails the read (3288)

        # Lazy backfill for NULL tsvectors: only pre-trigger tickets are NULL,
        # since the trigger fills every create and edit. Throttled; a full
        # batch re-arms so a backlog drains one batch per search (3290).
        run_if_due(
            "tsvector_backfill", conn, schema,
            lambda: self._backfill_null_tsvectors(conn, schema) >= self.TSVECTOR_BACKFILL_LIMIT,
        )

        cur = conn.cursor()
        tsquery_param = self._build_or_tsquery_param(query)

        use_fulltext = bool(tsquery_param)

        where_clauses: List[str] = []
        params: List[Any] = []

        if use_fulltext:
            where_clauses.append("content_tsvector @@ to_tsquery('english', %s)")
            params.append(tsquery_param)

        if not include_archived:
            where_clauses.append("archived_at IS NULL")

        if type_filter:
            where_clauses.append("type = %s")
            params.append(type_filter)

        if status_filter:
            where_clauses.append("status = %s")
            params.append(status_filter)

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        if use_fulltext:
            cur.execute(
                sql.SQL("""
                SELECT *, ts_rank(content_tsvector, to_tsquery('english', %s)) as rank
                FROM {}.tickets
                {}
                ORDER BY rank DESC
                LIMIT %s
                """).format(sql.Identifier(schema), sql.SQL(where_sql)),
                [tsquery_param] + params + [limit],
            )
        else:
            cur.execute(
                sql.SQL("""
                SELECT *
                FROM {}.tickets
                {}
                ORDER BY updated_at DESC
                LIMIT %s
                """).format(sql.Identifier(schema), sql.SQL(where_sql)),
                params + [limit],
            )
        rows = cur.fetchall()
        tickets = [self._row_to_response(r) for r in rows]
        if fields != "full":
            tickets = [self.to_card(t) for t in tickets]
        return SearchResult(
            tickets=tickets,
            total=len(rows),
            query=query,
            include_archived=include_archived,
        )

    # Card-excerpt length for board responses (chars). This bounds
    # description_preview ONLY — `description` is never cut: it travels whole
    # or (board fields="card", STOMPY-3284) not at all.
    BOARD_DESC_MAX_LENGTH = 100

    # Default limit per column in kanban/compact views.
    BOARD_DEFAULT_LIMIT = 10

    #: Appended when a preview is shorter than the description it excerpts.
    BOARD_DESC_ELLIPSIS = "..."

    @classmethod
    def to_card(cls, ticket: "TicketResponse") -> "TicketResponse":
        """CARD projection of a ticket (STOMPY-1923): the orientation shape
        list/search return by default — everything but the body and the
        sub-collections, plus the bounded description_preview. A pure
        copy; the source record is untouched. `get` returns the full record."""
        card = ticket.model_copy(
            update={"description": None, "history": [], "links": [], "context_links": []}
        )
        # Preview from the ORIGINAL body (the copy has none).
        if ticket.description:
            cutoff = cls.BOARD_DESC_MAX_LENGTH + len(cls.BOARD_DESC_ELLIPSIS)
            card.description_preview = (
                ticket.description[: cls.BOARD_DESC_MAX_LENGTH] + cls.BOARD_DESC_ELLIPSIS
                if len(ticket.description) > cutoff
                else ticket.description
            )
        return card

    def _attach_card_preview(self, ticket: "TicketResponse") -> None:
        """Set a bounded card excerpt WITHOUT ever mutating `description`.

        Contract, in full:
          * description is never cut — it is whole here; a card board
            (fields="card", STOMPY-3284) drops it to None afterwards, from
            a LEFT(description, cutoff + 1) prefix that decides the same.
          * description_preview is always set on board responses when there
            IS a description; it equals description when nothing was cut.
          * a None or empty description leaves description_preview None.
          * the excerpt is a literal prefix, never a paraphrase, and is
            never LONGER than the text it excerpts (adding the ellipsis to
            a 101-char description would produce 103 — sillier than just
            sending the original).

        History in STOMPY-1519: truncating description in place made the web
        detail dialog, which reuses the board row, render a cut string.
        """
        if not ticket.description:
            return
        cutoff = self.BOARD_DESC_MAX_LENGTH + len(self.BOARD_DESC_ELLIPSIS)
        if len(ticket.description) > cutoff:
            ticket.description_preview = (
                ticket.description[: self.BOARD_DESC_MAX_LENGTH] + self.BOARD_DESC_ELLIPSIS
            )
        else:
            ticket.description_preview = ticket.description

    # --- Batch operations --- #

    BATCH_MAX = 50  # Safety cap for batch operations

    def batch_transition(
        self,
        conn: DBConnection,
        schema: str,
        ticket_ids: List[int],
        target_status: str,
        confirm: bool = False,
        changed_by: Optional[str] = None,
        reason: Optional[str] = None,
        revisit_by: Optional[str] = None,
    ) -> BatchOperationResult:
        """Move multiple tickets to a target status.

        Two-phase: preview (default) shows what would happen, confirm=True executes.

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            ticket_ids: List of ticket IDs to transition.
            target_status: Target status for all tickets.
            confirm: If False (default), dry-run preview. If True, execute.
            changed_by: Who made the change.
            reason: One reason for the whole batch; REQUIRED for PARKED.
            revisit_by: Optional ISO date for a park.

        Returns:
            BatchOperationResult with per-ticket results.
        """
        if len(ticket_ids) > self.BATCH_MAX:
            return BatchOperationResult(
                action="batch_move",
                total=len(ticket_ids),
                succeeded=0,
                failed=len(ticket_ids),
                results=[BatchItemResult(
                    ticket_id=0, success=False,
                    error=f"Batch size {len(ticket_ids)} exceeds max {self.BATCH_MAX}",
                )],
                dry_run=not confirm,
            )
        if target_status == PARKED or reason is not None or revisit_by is not None:
            # Fail the whole batch up front rather than N identical per-row
            # refusals after N SELECTs.
            try:
                _validate_park_args(target_status, reason, revisit_by)
            except ParkArgumentError as e:
                return BatchOperationResult(
                    action="batch_move",
                    total=len(ticket_ids),
                    succeeded=0,
                    failed=len(ticket_ids),
                    results=[
                        BatchItemResult(ticket_id=tid, success=False, error=str(e))
                        for tid in ticket_ids
                    ],
                    dry_run=not confirm,
                )

        results: List[BatchItemResult] = []
        succeeded = 0
        failed = 0

        for tid in ticket_ids:
            cur = conn.cursor()
            cur.execute(
                sql.SQL("SELECT type, status FROM {}.tickets WHERE id = %s").format(
                    sql.Identifier(schema)
                ),
                (tid,),
            )
            row = cur.fetchone()

            if not row:
                results.append(BatchItemResult(
                    ticket_id=tid, success=False, error="Ticket not found",
                ))
                failed += 1
                continue

            old_status = row["status"]
            ticket_type = row["type"]

            # Check if transition is valid
            if not validate_transition(ticket_type, old_status, target_status, raise_on_invalid=False):
                sm = STATE_MACHINES.get(ticket_type, {})
                allowed = sm.get("transitions", {}).get(old_status, [])
                results.append(BatchItemResult(
                    ticket_id=tid, success=False,
                    old_status=old_status,
                    error=f"Cannot transition {ticket_type} from '{old_status}' to '{target_status}'. Allowed: {allowed}",
                ))
                failed += 1
                continue

            if confirm:
                try:
                    self.transition_ticket(
                        conn, schema, tid, target_status, changed_by,
                        reason=reason, revisit_by=revisit_by,
                    )
                    results.append(BatchItemResult(
                        ticket_id=tid, success=True,
                        old_status=old_status, new_status=target_status,
                    ))
                    succeeded += 1
                except Exception as e:
                    results.append(BatchItemResult(
                        ticket_id=tid, success=False,
                        old_status=old_status, error=str(e),
                    ))
                    failed += 1
            else:
                # Dry-run: report what would happen
                results.append(BatchItemResult(
                    ticket_id=tid, success=True,
                    old_status=old_status, new_status=target_status,
                ))
                succeeded += 1

        return BatchOperationResult(
            action="batch_move",
            total=len(ticket_ids),
            succeeded=succeeded,
            failed=failed,
            results=results,
            dry_run=not confirm,
        )

    def batch_close(
        self,
        conn: DBConnection,
        schema: str,
        ticket_ids: List[int],
        confirm: bool = False,
        changed_by: Optional[str] = None,
        resolution: Optional[str] = None,
    ) -> BatchOperationResult:
        """Close multiple tickets, auto-walking intermediate states.

        For each ticket, finds a path to a terminal status via repeated
        transition_ticket() calls. Two-phase: preview/confirm.

        Prefers positive terminal (resolved, done, shipped) by default.
        Pass resolution to override (e.g. "wont_fix", "cancelled").

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            ticket_ids: List of ticket IDs to close.
            confirm: If False (default), dry-run preview. If True, execute.
            changed_by: Who closed the tickets.
            resolution: Specific terminal status to target. If None, uses
                the positive default for each ticket's type.

        Returns:
            BatchOperationResult with per-ticket results.
        """
        if len(ticket_ids) > self.BATCH_MAX:
            return BatchOperationResult(
                action="batch_close",
                total=len(ticket_ids),
                succeeded=0,
                failed=len(ticket_ids),
                results=[BatchItemResult(
                    ticket_id=0, success=False,
                    error=f"Batch size {len(ticket_ids)} exceeds max {self.BATCH_MAX}",
                )],
                dry_run=not confirm,
            )

        results: List[BatchItemResult] = []
        succeeded = 0
        failed = 0

        for tid in ticket_ids:
            cur = conn.cursor()
            cur.execute(
                sql.SQL("SELECT type, status FROM {}.tickets WHERE id = %s").format(
                    sql.Identifier(schema)
                ),
                (tid,),
            )
            row = cur.fetchone()

            if not row:
                results.append(BatchItemResult(
                    ticket_id=tid, success=False, error="Ticket not found",
                ))
                failed += 1
                continue

            ticket_type = row["type"]
            current_status = row["status"]
            terminals = get_terminal_statuses(ticket_type)

            # Already closed
            if current_status in terminals:
                results.append(BatchItemResult(
                    ticket_id=tid, success=True,
                    old_status=current_status, new_status=current_status,
                ))
                succeeded += 1
                continue

            # Validate resolution for this ticket type if provided
            if resolution and resolution not in terminals:
                results.append(BatchItemResult(
                    ticket_id=tid, success=False,
                    old_status=current_status,
                    error=f"'{resolution}' is not a terminal status for {ticket_type}. "
                          f"Valid: {terminals}",
                ))
                failed += 1
                continue

            # Find path to terminal via BFS-like walk
            path = self._find_close_path(ticket_type, current_status, resolution)
            if not path:
                results.append(BatchItemResult(
                    ticket_id=tid, success=False,
                    old_status=current_status,
                    error=f"No path to terminal from '{current_status}' for {ticket_type}",
                ))
                failed += 1
                continue

            final_status = path[-1]

            if confirm:
                try:
                    for step_status in path:
                        self.transition_ticket(conn, schema, tid, step_status, changed_by)
                    results.append(BatchItemResult(
                        ticket_id=tid, success=True,
                        old_status=current_status, new_status=final_status,
                    ))
                    succeeded += 1
                except Exception as e:
                    results.append(BatchItemResult(
                        ticket_id=tid, success=False,
                        old_status=current_status, error=str(e),
                    ))
                    failed += 1
            else:
                results.append(BatchItemResult(
                    ticket_id=tid, success=True,
                    old_status=current_status, new_status=final_status,
                ))
                succeeded += 1

        return BatchOperationResult(
            action="batch_close",
            total=len(ticket_ids),
            succeeded=succeeded,
            failed=failed,
            results=results,
            dry_run=not confirm,
        )

    @staticmethod
    def _find_close_path(
        ticket_type: str,
        current_status: str,
        target_terminal: Optional[str] = None,
    ) -> Optional[List[str]]:
        """Find shortest path from current_status to a terminal status.

        Uses BFS over the state machine transitions. When no target is
        specified, prefers the positive terminal (e.g. resolved > wont_fix).

        Args:
            ticket_type: The ticket type.
            current_status: Current status.
            target_terminal: Specific terminal to reach. If None, uses the
                positive default from POSITIVE_TERMINALS.

        Returns:
            List of statuses to transition through (not including current),
            or None if no path exists.
        """
        sm = STATE_MACHINES.get(ticket_type)
        if not sm:
            return None

        terminals = set(sm["terminal"])
        transitions = sm["transitions"]

        # Determine which terminal to target
        preferred = target_terminal or POSITIVE_TERMINALS.get(ticket_type)

        # Already at a terminal status — no transitions needed
        if current_status in terminals:
            return None

        # Strategy: find path to preferred terminal first; fall back to any
        from collections import deque

        def _bfs_to(target_set: set) -> Optional[list]:
            queue: deque = deque()
            queue.append((current_status, []))
            visited = {current_status}
            while queue:
                status, path = queue.popleft()
                for next_status in transitions.get(status, []):
                    if next_status in visited:
                        continue
                    new_path = path + [next_status]
                    if next_status in target_set:
                        return new_path
                    visited.add(next_status)
                    queue.append((next_status, new_path))
            return None

        # Try preferred terminal first
        if preferred and preferred in terminals:
            path = _bfs_to({preferred})
            if path:
                return path

        # Fall back to shortest path to any terminal
        return _bfs_to(terminals)

    # --- Link operations --- #

    def add_link(
        self,
        conn: DBConnection,
        schema: str,
        source_id: int,
        data: TicketLinkCreate,
    ) -> TicketLinkResponse:
        """Add a link between two tickets.

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            source_id: Source ticket ID.
            data: Link creation data.

        Returns:
            Created link response.
        """
        if data.target_id == source_id:
            raise LinkRefusedError(
                "SELF_LINK",
                f"Ticket #{source_id} cannot be linked to itself; "
                "pick a different target_id.",
            )
        cur = conn.cursor()
        try:
            if data.link_type == LinkType.blocks:
                # Direct 2-cycle only (STOMPY-2871); longer cycles are not checked.
                cur.execute(
                    sql.SQL("""
                    SELECT 1 FROM {}.ticket_links
                    WHERE source_id = %s AND target_id = %s AND link_type = 'blocks'
                    LIMIT 1
                    """).format(sql.Identifier(schema)),
                    (data.target_id, source_id),
                )
                if cur.fetchone():
                    raise LinkRefusedError(
                        "LINK_CYCLE",
                        f"Ticket #{data.target_id} already blocks #{source_id}; "
                        f"#{source_id} blocking #{data.target_id} would make a "
                        "dependency cycle that neither could ever leave. "
                        "Remove the existing link first if the direction was wrong.",
                    )
            if data.link_type in _SYMMETRIC_LINK_TYPES:
                # A related B and B related A are one link (STOMPY-2880).
                existing = self._find_link_id(
                    cur, schema, data.target_id, source_id, data.link_type.value
                )
                if existing is not None:
                    raise LinkAlreadyExistsError(
                        f"Link already exists between ticket #{source_id} and "
                        f"#{data.target_id}",
                        link_id=existing,
                    )
            now = time.time()
            cur.execute(
                sql.SQL("""
                INSERT INTO {}.ticket_links
                    (source_id, target_id, link_type, created_at)
                VALUES (%s, %s, %s, %s)
                RETURNING *
                """).format(sql.Identifier(schema)),
                (source_id, data.target_id, data.link_type.value, now),
            )
            row = cur.fetchone()

            # Enrich with target ticket title/status so the response
            # matches the format returned by list_links / _get_links_for_ticket
            cur.execute(
                sql.SQL("SELECT title, status FROM {}.tickets WHERE id = %s").format(
                    sql.Identifier(schema)
                ),
                (data.target_id,),
            )
            target = cur.fetchone()
            if target:
                row["target_title"] = target["title"]
                row["target_status"] = target["status"]

            conn.commit()
            return self._link_row_to_response(row, source_id)
        except LinkAlreadyExistsError:
            conn.rollback()
            raise
        except IntegrityError:
            conn.rollback()
            try:  # best-effort: the id is a courtesy, the refusal is the point
                existing = self._find_link_id(
                    cur, schema, source_id, data.target_id, data.link_type.value
                )
            except Exception:
                conn.rollback()
                existing = None
            raise LinkAlreadyExistsError(
                f"Link already exists between ticket #{source_id} and #{data.target_id}",
                link_id=existing,
            )
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _find_link_id(cur, schema: str, source: int, target: int, link_type: str):
        cur.execute(
            sql.SQL("""
            SELECT id FROM {}.ticket_links
            WHERE source_id = %s AND target_id = %s AND link_type = %s
            LIMIT 1
            """).format(sql.Identifier(schema)),
            (source, target, link_type),
        )
        row = cur.fetchone()
        return row["id"] if row else None

    def remove_link(
        self,
        conn: DBConnection,
        schema: str,
        link_id: int,
        ticket_id: Optional[int] = None,
    ) -> bool:
        """Remove a ticket link.

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            link_id: Link ID.
            ticket_id: When given (STOMPY-2985), the link is removed only if this
                ticket is its source or target; otherwise it is "not found" and
                nothing is deleted.

        Returns:
            True if deleted, False if not found (or not on ``ticket_id``).
        """
        cur = conn.cursor()
        try:
            cur.execute(
                sql.SQL(
                    "DELETE FROM {}.ticket_links WHERE id = %s "
                    "AND (%s::int IS NULL OR source_id = %s OR target_id = %s) RETURNING id"
                ).format(sql.Identifier(schema)),
                (link_id, ticket_id, ticket_id, ticket_id),
            )
            result = cur.fetchone()
            conn.commit()
            return result is not None
        except Exception:
            conn.rollback()
            raise

    def list_links(
        self,
        conn: DBConnection,
        schema: str,
        ticket_id: int,
    ) -> List[TicketLinkResponse]:
        """List all links for a ticket (both directions).

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            ticket_id: Ticket ID.

        Returns:
            List of link responses.
        """
        cur = conn.cursor()
        return self._get_links_for_ticket(cur, schema, ticket_id)

    def list_all_links(
        self,
        conn: DBConnection,
        schema: str,
        ticket_id: int,
    ) -> List[TicketLinkResponse]:
        """Ticket links (kind='ticket') then context links (kind='context').

        STOMPY-2858: the REST list shows both. list_links stays ticket-only
        because the MCP list action reports context links separately.
        """
        rows = self.list_links(conn, schema, ticket_id)
        for c in self.list_context_links_for_ticket(conn, schema, ticket_id):
            rows.append(
                TicketLinkResponse(
                    id=c.id,
                    kind="context",
                    ticket_id=c.ticket_id,
                    context_label=c.context_label,
                    context_version=c.context_version,
                    link_type=c.link_type,
                    created_at=c.created_at,
                    direction="outgoing",
                    relation=c.link_type,
                )
            )
        return rows

    # --- Context link methods --- #

    def add_context_link(
        self,
        conn: DBConnection,
        schema: str,
        ticket_id: int,
        data: ContextLinkCreate,
    ) -> ContextLinkResponse:
        """Add a link from a ticket to a context.

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            ticket_id: Source ticket ID.
            data: Context link creation data.

        Returns:
            Created context link response.
        """
        cur = conn.cursor()
        try:
            now = time.time()
            cur.execute(
                sql.SQL("""
                INSERT INTO {}.ticket_context_links
                    (ticket_id, context_label, context_version, link_type, created_at)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """).format(sql.Identifier(schema)),
                (ticket_id, data.context_label, data.context_version, data.link_type.value, now),
            )
            row = cur.fetchone()

            # Enrich with ticket title/status for display
            cur.execute(
                sql.SQL("SELECT title, status FROM {}.tickets WHERE id = %s").format(
                    sql.Identifier(schema)
                ),
                (ticket_id,),
            )
            ticket = cur.fetchone()
            conn.commit()
            return ContextLinkResponse(
                id=row["id"],
                ticket_id=row["ticket_id"],
                context_label=row["context_label"],
                context_version=row["context_version"],
                link_type=row["link_type"],
                created_at=row.get("created_at"),
                ticket_title=ticket["title"] if ticket else None,
                ticket_status=ticket["status"] if ticket else None,
            )
        except IntegrityError:
            conn.rollback()
            raise LinkAlreadyExistsError(
                f"Context link already exists: ticket #{ticket_id} ↔ {data.context_label}"
            )
        except Exception:
            conn.rollback()
            raise

    def remove_context_link(
        self,
        conn: DBConnection,
        schema: str,
        link_id: int,
        ticket_id: Optional[int] = None,
    ) -> bool:
        """Remove a ticket↔context link.

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            link_id: Context link ID.
            ticket_id: When given (STOMPY-2985), only a link hanging on this ticket
                is removed.

        Returns:
            True if deleted, False if not found (or not on ``ticket_id``).
        """
        cur = conn.cursor()
        try:
            cur.execute(
                sql.SQL(
                    "DELETE FROM {}.ticket_context_links WHERE id = %s "
                    "AND (%s::int IS NULL OR ticket_id = %s) RETURNING id"
                ).format(sql.Identifier(schema)),
                (link_id, ticket_id, ticket_id),
            )
            result = cur.fetchone()
            conn.commit()
            return result is not None
        except Exception:
            conn.rollback()
            raise

    def list_context_links_for_ticket(
        self,
        conn: DBConnection,
        schema: str,
        ticket_id: int,
    ) -> List[ContextLinkResponse]:
        """List all context links for a ticket.

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            ticket_id: Ticket ID.

        Returns:
            List of context link responses.
        """
        cur = conn.cursor()
        cur.execute(
            sql.SQL("""
            SELECT tcl.*, t.title AS ticket_title, t.status AS ticket_status
            FROM {sch}.ticket_context_links tcl
            JOIN {sch}.tickets t ON t.id = tcl.ticket_id
            WHERE tcl.ticket_id = %s
            ORDER BY tcl.created_at DESC
            """).format(sch=sql.Identifier(schema)),
            (ticket_id,),
        )
        rows = cur.fetchall()
        return [
            ContextLinkResponse(
                id=r["id"],
                ticket_id=r["ticket_id"],
                context_label=r["context_label"],
                context_version=r["context_version"],
                link_type=r["link_type"],
                created_at=r.get("created_at"),
                ticket_title=r.get("ticket_title"),
                ticket_status=r.get("ticket_status"),
            )
            for r in rows
        ]

    def list_tickets_for_context(
        self,
        conn: DBConnection,
        schema: str,
        context_label: str,
    ) -> List[ContextLinkResponse]:
        """List all tickets linked to a given context label.

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            context_label: Context label to look up.

        Returns:
            List of context link responses (with ticket info denormalized).
        """
        cur = conn.cursor()
        cur.execute(
            sql.SQL("""
            SELECT tcl.*, t.title AS ticket_title, t.status AS ticket_status
            FROM {sch}.ticket_context_links tcl
            JOIN {sch}.tickets t ON t.id = tcl.ticket_id
            WHERE tcl.context_label = %s
            ORDER BY tcl.created_at DESC
            LIMIT 10
            """).format(sch=sql.Identifier(schema)),
            (context_label,),
        )
        rows = cur.fetchall()
        return [
            ContextLinkResponse(
                id=r["id"],
                ticket_id=r["ticket_id"],
                context_label=r["context_label"],
                context_version=r["context_version"],
                link_type=r["link_type"],
                created_at=r.get("created_at"),
                ticket_title=r.get("ticket_title"),
                ticket_status=r.get("ticket_status"),
            )
            for r in rows
        ]

    # --- Helpers --- #

    def _fetch_history(self, cur, schema: str, ticket_id: int) -> List[TicketHistoryEntry]:
        """Fetch history entries for a ticket, ordered newest first.

        Args:
            cur: Database cursor (RealDictCursor).
            schema: PostgreSQL schema name.
            ticket_id: Ticket ID.

        Returns:
            List of TicketHistoryEntry objects.
        """
        cur.execute(
            sql.SQL("""
            SELECT * FROM {}.ticket_history
            WHERE ticket_id = %s ORDER BY changed_at DESC
            """).format(sql.Identifier(schema)),
            (ticket_id,),
        )
        return [
            TicketHistoryEntry(
                id=h["id"],
                field_name=h["field_name"],
                old_value=h["old_value"],
                new_value=h["new_value"],
                changed_by=h["changed_by"],
                changed_at=h["changed_at"],
            )
            for h in cur.fetchall()
        ]

    def _get_links_for_tickets_bulk(
        self, cur: Any, schema: str, ticket_ids: List[int]
    ) -> Dict[int, List[TicketLinkResponse]]:
        """Bulk-fetch ticket↔ticket links for many tickets in a single query.

        Returns a mapping of ticket_id → list of links. A link is included
        under whichever side of the relationship matches an input id; if both
        sides match, it appears under both.
        """
        if not ticket_ids:
            return {}
        sch = sql.Identifier(schema)
        placeholders = sql.SQL(", ").join(sql.Placeholder() * len(ticket_ids))
        cur.execute(
            sql.SQL("""
            SELECT tl.*,
                   src.title AS source_title, src.status AS source_status,
                   tgt.title AS target_title, tgt.status AS target_status
            FROM {}.ticket_links tl
            JOIN {}.tickets src ON src.id = tl.source_id
            JOIN {}.tickets tgt ON tgt.id = tl.target_id
            WHERE tl.source_id IN ({ph}) OR tl.target_id IN ({ph})
            """).format(sch, sch, sch, ph=placeholders),
            ticket_ids + ticket_ids,
        )
        result: Dict[int, List[TicketLinkResponse]] = {tid: [] for tid in ticket_ids}
        for row in cur.fetchall():
            # Attribute the link to the input ticket on whichever side matches,
            # oriented to that viewer (STOMPY-2843).
            if row["source_id"] in result:
                result[row["source_id"]].append(self._link_row_to_response(row, row["source_id"]))
            if row["target_id"] in result and row["target_id"] != row["source_id"]:
                result[row["target_id"]].append(self._link_row_to_response(row, row["target_id"]))
        return result

    def _get_context_links_for_tickets_bulk(
        self, cur: Any, schema: str, ticket_ids: List[int]
    ) -> Dict[int, List[ContextLinkResponse]]:
        """Bulk-fetch ticket↔context links for many tickets in a single query."""
        if not ticket_ids:
            return {}
        sch = sql.Identifier(schema)
        placeholders = sql.SQL(", ").join(sql.Placeholder() * len(ticket_ids))
        # The caller asks context_links_present first (STOMPY-3296).
        cur.execute(
            sql.SQL("""
            SELECT tcl.*, t.title AS ticket_title, t.status AS ticket_status
            FROM {}.ticket_context_links tcl
            JOIN {}.tickets t ON t.id = tcl.ticket_id
            WHERE tcl.ticket_id IN ({ph})
            ORDER BY tcl.created_at DESC
            """).format(sch, sch, ph=placeholders),
            ticket_ids,
        )
        result: Dict[int, List[ContextLinkResponse]] = {tid: [] for tid in ticket_ids}
        for r in cur.fetchall():
            tid = r["ticket_id"]
            if tid in result:
                result[tid].append(
                    ContextLinkResponse(
                        id=r["id"],
                        ticket_id=tid,
                        context_label=r["context_label"],
                        context_version=r["context_version"],
                        link_type=r["link_type"],
                        created_at=r.get("created_at"),
                        ticket_title=r.get("ticket_title"),
                        ticket_status=r.get("ticket_status"),
                    )
                )
        return result

    def _get_links_for_ticket(
        self, cur: Any, schema: str, ticket_id: int
    ) -> List[TicketLinkResponse]:
        """Get all links for a ticket (both as source and target)."""
        sch = sql.Identifier(schema)
        cur.execute(
            sql.SQL("""
            SELECT tl.*,
                   src.title AS source_title, src.status AS source_status,
                   tgt.title AS target_title, tgt.status AS target_status
            FROM {}.ticket_links tl
            JOIN {}.tickets src ON src.id = tl.source_id
            JOIN {}.tickets tgt ON tgt.id = tl.target_id
            WHERE tl.source_id = %s OR tl.target_id = %s
            """).format(sch, sch, sch),
            (ticket_id, ticket_id),
        )
        return [self._link_row_to_response(r, ticket_id) for r in cur.fetchall()]

    def _row_to_response(self, row: Dict[str, Any]) -> TicketResponse:
        """Convert a database row to a TicketResponse."""
        from stompy_ticketing.ticket_projection import row_to_response

        return row_to_response(row)

    def _link_row_to_response(
        self, row: Dict[str, Any], viewed_id: Optional[int] = None
    ) -> TicketLinkResponse:
        """Convert a link row, oriented to the ticket being read (STOMPY-2843).

        source_*/target_* are always honest about source_id/target_id.
        peer_*, direction and relation describe the COUNTERPART of
        viewed_id: outgoing when viewed_id is the source (or on a
        self-link), incoming otherwise. Without viewed_id, viewed is the
        source.
        """
        src_id, tgt_id = row["source_id"], row["target_id"]
        outgoing = viewed_id is None or viewed_id == src_id
        link_type = row["link_type"]
        return TicketLinkResponse(
            id=row["id"],
            source_id=src_id,
            target_id=tgt_id,
            link_type=link_type,
            created_at=row.get("created_at"),
            source_title=row.get("source_title"),
            source_status=row.get("source_status"),
            target_title=row.get("target_title"),
            target_status=row.get("target_status"),
            direction="outgoing" if outgoing else "incoming",
            relation=link_type if outgoing or link_type != "blocks" else "blocked_by",
            peer_id=tgt_id if outgoing else src_id,
            peer_title=row.get("target_title") if outgoing else row.get("source_title"),
            peer_status=row.get("target_status") if outgoing else row.get("source_status"),
        )
