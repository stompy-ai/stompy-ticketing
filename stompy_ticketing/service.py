"""TicketService - business logic and state machine for stompy-ticketing.

Follows the ContextService pattern from dementia-production:
- Takes a DB connection/adapter as parameter (DI)
- Sync methods with psycopg2 RealDictCursor
- TEXT for JSON strings (tags, metadata), DOUBLE PRECISION timestamps
"""

import hashlib
import json
import re
import time
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from psycopg2 import IntegrityError, sql

from stompy_ticketing.models import (
    BatchItemResult,
    BatchOperationResult,
    BoardColumn,
    BoardView,
    CompactTicket,
    ContextLinkCreate,
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


class LinkAlreadyExistsError(ValueError):
    """Raised when add_link/add_context_link hits a duplicate UNIQUE constraint.

    Subclasses ValueError so existing callers that catch ValueError keep
    working; new callers can catch this specifically to return a structured
    `ALREADY_LINKED` error code instead of a raw IntegrityError.
    """

    pass


# Each type maps to: initial status, terminal statuses, and allowed transitions
STATE_MACHINES: Dict[str, Dict[str, Any]] = {
    "task": {
        "initial": "backlog",
        "terminal": ["done", "cancelled"],
        "transitions": {
            "backlog": ["in_progress", "done", "cancelled"],
            "in_progress": ["done", "cancelled"],
            "done": ["backlog"],
            "cancelled": ["backlog"],
        },
    },
    "bug": {
        "initial": "triage",
        "terminal": ["resolved", "wont_fix"],
        "transitions": {
            "triage": ["confirmed", "wont_fix"],
            "confirmed": ["in_progress"],
            "in_progress": ["resolved", "wont_fix"],
            "resolved": ["triage"],
            "wont_fix": ["triage"],
        },
    },
    "feature": {
        "initial": "proposed",
        "terminal": ["shipped", "rejected"],
        "transitions": {
            "proposed": ["approved", "rejected"],
            "approved": ["in_progress"],
            "in_progress": ["shipped", "rejected"],
            "shipped": ["proposed"],
            "rejected": ["proposed"],
        },
    },
    "decision": {
        "initial": "open",
        "terminal": ["decided", "deferred"],
        "transitions": {
            "open": ["decided", "deferred"],
            "decided": ["open"],
            "deferred": ["open"],  # Deferred decisions can be reopened
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
            raise InvalidTransitionError(
                f"Cannot transition {ticket_type} from '{current_status}' to "
                f"'{target_status}'. Allowed: {allowed}"
            )
        return False

    return True


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


class TicketService:
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

        # Content hash for deduplication
        content = f"{data.title}|{data.description or ''}"
        content_hash = hashlib.sha256(content.encode()).hexdigest()[:16]

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
            # Also fetch context links
            try:
                response.context_links = self.list_context_links_for_ticket(
                    conn, schema, ticket_id
                )
            except Exception:
                pass  # Table may not exist yet

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

        Returns:
            Updated ticket or None if not found.
        """
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
            if updated_row is None and expected_updated_at is not None:
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
                    expected_updated_at, row.get("updated_at") or 0.0
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
    ) -> Optional[TicketResponse]:
        """Transition a ticket to a new status via the state machine.

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            ticket_id: Ticket ID.
            target_status: Target status.
            changed_by: Who made the change.

        Returns:
            Updated ticket or None if not found.

        Raises:
            InvalidTransitionError: If transition is not allowed.
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

            cur.execute(
                sql.SQL("""
                UPDATE {}.tickets
                SET status = %s, updated_at = %s, closed_at = %s
                WHERE id = %s
                RETURNING *
                """).format(sql.Identifier(schema)),
                (target_status, now, closed_at, ticket_id),
            )
            updated_row = cur.fetchone()

            # Record history
            cur.execute(
                sql.SQL("""
                INSERT INTO {}.ticket_history
                    (ticket_id, field_name, old_value, new_value, changed_by, changed_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                """).format(sql.Identifier(schema)),
                (ticket_id, "status", current_status, target_status, changed_by, now),
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

    def archive_stale_tickets(
        self,
        conn: DBConnection,
        schema: str,
        ttl_seconds: int = 1_209_600,
    ) -> int:
        """Archive tickets in terminal status past the TTL.

        Sets archived_at on tickets where closed_at < now - ttl and
        archived_at IS NULL. Records history entries for each.

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            ttl_seconds: Seconds after close before archival (default 14 days).

        Returns:
            Number of tickets archived.
        """
        cur = conn.cursor()
        try:
            now = time.time()
            cutoff = now - ttl_seconds

            # Find stale tickets not yet archived
            all_terminals = get_all_terminal_statuses()
            terminal_placeholders = ", ".join(["%s"] * len(all_terminals))

            cur.execute(
                sql.SQL("""
                SELECT id, type, status FROM {}.tickets
                WHERE closed_at IS NOT NULL
                  AND closed_at < %s
                  AND archived_at IS NULL
                  AND status IN ({})
                """).format(
                    sql.Identifier(schema),
                    sql.SQL(terminal_placeholders),
                ),
                [cutoff] + all_terminals,
            )
            stale = cur.fetchall()

            if not stale:
                return 0

            stale_ids = [r["id"] for r in stale]
            id_placeholders = ", ".join(["%s"] * len(stale_ids))

            # Batch update archived_at
            cur.execute(
                sql.SQL("""
                UPDATE {}.tickets
                SET archived_at = %s
                WHERE id IN ({})
                """).format(
                    sql.Identifier(schema),
                    sql.SQL(id_placeholders),
                ),
                [now] + stale_ids,
            )

            # Record history entries
            for ticket in stale:
                cur.execute(
                    sql.SQL("""
                    INSERT INTO {}.ticket_history
                        (ticket_id, field_name, old_value, new_value, changed_by, changed_at)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """).format(sql.Identifier(schema)),
                    (ticket["id"], "archived_at", None, str(now), "system:auto_archive", now),
                )

            conn.commit()
            return len(stale_ids)
        except Exception:
            conn.rollback()
            raise

    def list_tickets(
        self,
        conn: DBConnection,
        schema: str,
        filters: Optional[TicketListFilters] = None,
    ) -> TicketListResponse:
        """List tickets with optional filters.

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            filters: Filter/pagination options.

        Returns:
            List of tickets with counts.
        """
        if filters is None:
            filters = TicketListFilters()

        # Lazy archive trigger
        try:
            self.archive_stale_tickets(conn, schema)
        except Exception:
            pass  # Archive failure should not break list

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

        return TicketListResponse(
            tickets=[self._row_to_response(r) for r in rows],
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

    def _backfill_null_tsvectors(
        self, conn: DBConnection, schema: str, limit: int = 200
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
    ) -> SearchResult:
        """Full-text search tickets using tsvector with OR-based ranking.

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
        # Lazy archive trigger
        try:
            self.archive_stale_tickets(conn, schema)
        except Exception:
            pass

        # Lazy backfill for NULL tsvectors (handles pre-trigger tickets)
        try:
            self._backfill_null_tsvectors(conn, schema)
        except Exception:
            pass

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

        return SearchResult(
            tickets=[self._row_to_response(r) for r in rows],
            total=len(rows),
            query=query,
            include_archived=include_archived,
        )

    # Card-excerpt length for board responses (chars). This bounds
    # description_preview ONLY — `description` itself always travels whole.
    BOARD_DESC_MAX_LENGTH = 100

    # Default limit per column in kanban/compact views.
    BOARD_DEFAULT_LIMIT = 10

    #: Appended when a preview is shorter than the description it excerpts.
    BOARD_DESC_ELLIPSIS = "..."

    def _attach_card_preview(self, ticket: "TicketResponse") -> None:
        """Set a bounded card excerpt WITHOUT ever mutating `description`.

        Contract, in full:
          * description is never modified — board rows carry it whole.
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

    def board_view(
        self,
        conn: DBConnection,
        schema: str,
        type_filter: Optional[str] = None,
        view: str = "kanban",
        status_filter: Optional[str] = None,
        include_terminal: bool = False,
        include_archived: bool = False,
        limit: Optional[int] = None,
    ) -> BoardView:
        """Get a kanban board view of tickets grouped by status.

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            type_filter: Filter by ticket type.
            view: "kanban" (tickets with truncated descriptions),
                  "summary" (counts only), "compact" (id/title/priority only),
                  or "detail" (alias for kanban).
            status_filter: Filter by status (e.g., "triage", "backlog").
            include_terminal: Include terminal statuses (default False).
            include_archived: Include archived tickets (default False).
            limit: Max tickets per column (default 10 for kanban/compact).
                   Use 0 for unlimited.

        Returns:
            Board view with columns.
        """
        # Lazy archive trigger
        try:
            self.archive_stale_tickets(conn, schema)
        except Exception:
            pass

        cur = conn.cursor()
        conditions: List[str] = []
        params: List[Any] = []

        if not include_archived:
            conditions.append("archived_at IS NULL")

        if type_filter:
            conditions.append("type = %s")
            params.append(type_filter)

        if status_filter:
            # Explicit status filter — don't add terminal exclusion
            conditions.append("status = %s")
            params.append(status_filter)
        elif not include_terminal:
            # Exclude terminal statuses
            if type_filter and type_filter in STATE_MACHINES:
                terminals = get_terminal_statuses(type_filter)
            else:
                terminals = get_all_terminal_statuses()
            if terminals:
                placeholders = ", ".join(["%s"] * len(terminals))
                conditions.append(f"status NOT IN ({placeholders})")
                params.extend(terminals)

        where_sql = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        # Get archived count for display
        archived_count = 0
        try:
            cur.execute(
                sql.SQL(
                    "SELECT COUNT(*) as count FROM {}.tickets WHERE archived_at IS NOT NULL"
                ).format(sql.Identifier(schema)),
            )
            archived_count = cur.fetchone()["count"]
        except Exception:
            pass

        if view == "summary":
            cur.execute(
                sql.SQL("""
                SELECT status, COUNT(*) as count
                FROM {}.tickets {}
                GROUP BY status ORDER BY status
                """).format(sql.Identifier(schema), sql.SQL(where_sql)),
                params,
            )
            rows = cur.fetchall()
            columns = [
                BoardColumn(status=r["status"], count=r["count"], tickets=[])
                for r in rows
            ]
            total = sum(r["count"] for r in rows)
        else:
            # Kanban / compact / detail — get tickets grouped by status
            is_compact = view == "compact"

            # Determine effective limit per column
            if limit is not None:
                effective_limit = None if limit == 0 else max(1, limit)
            else:
                effective_limit = self.BOARD_DEFAULT_LIMIT

            # For compact view, only fetch needed columns
            if is_compact:
                select_cols = "id, title, type, status, priority, assignee"
            else:
                select_cols = "*"

            cur.execute(
                sql.SQL("""
                SELECT {} FROM {}.tickets {}
                ORDER BY
                    CASE priority
                        WHEN 'urgent' THEN 0
                        WHEN 'high' THEN 1
                        WHEN 'medium' THEN 2
                        WHEN 'low' THEN 3
                        ELSE 4
                    END,
                    updated_at DESC
                """).format(
                    sql.SQL(select_cols),
                    sql.Identifier(schema),
                    sql.SQL(where_sql),
                ),
                params,
            )
            rows = cur.fetchall()

            # Group by status
            status_groups: Dict[str, List] = {}
            for r in rows:
                status = r["status"]
                if status not in status_groups:
                    status_groups[status] = []
                status_groups[status].append(r)

            # Build ordered columns based on type's state machine
            if type_filter and type_filter in STATE_MACHINES:
                ordered_statuses = list(STATE_MACHINES[type_filter]["transitions"].keys())
            else:
                ordered_statuses = sorted(status_groups.keys())

            columns = []
            for status in ordered_statuses:
                group_rows = status_groups.get(status, [])
                total_in_col = len(group_rows)
                # Apply per-column limit
                if effective_limit is not None:
                    visible_rows = group_rows[:effective_limit]
                else:
                    visible_rows = group_rows
                has_more = total_in_col > len(visible_rows)

                if is_compact:
                    compact_tickets = [
                        CompactTicket(
                            id=r["id"],
                            title=r["title"],
                            type=r["type"],
                            status=r["status"],
                            priority=r["priority"],
                            assignee=r.get("assignee"),
                        )
                        for r in visible_rows
                    ]
                    columns.append(
                        BoardColumn(
                            status=status,
                            count=total_in_col,
                            compact_tickets=compact_tickets,
                            has_more=has_more,
                            truncated_count=total_in_col - len(visible_rows),
                        )
                    )
                else:
                    tickets = []
                    for r in visible_rows:
                        ticket = self._row_to_response(r)
                        self._attach_card_preview(ticket)
                        tickets.append(ticket)
                    columns.append(
                        BoardColumn(
                            status=status,
                            count=total_in_col,
                            tickets=tickets,
                            has_more=has_more,
                            truncated_count=total_in_col - len(visible_rows),
                        )
                    )

            # Add any statuses not in the ordered list
            for status in status_groups:
                if status not in ordered_statuses:
                    group_rows = status_groups[status]
                    total_in_col = len(group_rows)
                    if effective_limit is not None:
                        visible_rows = group_rows[:effective_limit]
                    else:
                        visible_rows = group_rows
                    has_more = total_in_col > len(visible_rows)

                    if is_compact:
                        compact_tickets = [
                            CompactTicket(
                                id=r["id"],
                                title=r["title"],
                                type=r["type"],
                                status=r["status"],
                                priority=r["priority"],
                                assignee=r.get("assignee"),
                            )
                            for r in visible_rows
                        ]
                        columns.append(
                            BoardColumn(
                                status=status,
                                count=total_in_col,
                                compact_tickets=compact_tickets,
                                has_more=has_more,
                                truncated_count=total_in_col - len(visible_rows),
                            )
                        )
                    else:
                        tickets = []
                        for r in visible_rows:
                            ticket = self._row_to_response(r)
                            self._attach_card_preview(ticket)
                            tickets.append(ticket)
                        columns.append(
                            BoardColumn(
                                status=status,
                                count=total_in_col,
                                tickets=tickets,
                                has_more=has_more,
                                truncated_count=total_in_col - len(visible_rows),
                            )
                        )

            total = len(rows)

            # Bug #173: populate per-card links on kanban view (not compact/summary).
            # Single bulk query for all visible ticket IDs avoids N+1.
            if not is_compact:
                visible_ids = [t.id for col in columns for t in col.tickets]
                if visible_ids:
                    try:
                        link_map = self._get_links_for_tickets_bulk(cur, schema, visible_ids)
                        ctx_link_map = self._get_context_links_for_tickets_bulk(
                            cur, schema, visible_ids
                        )
                    except Exception:
                        link_map = {}
                        ctx_link_map = {}
                    for col in columns:
                        for t in col.tickets:
                            t.links = link_map.get(t.id, [])
                            t.context_links = ctx_link_map.get(t.id, [])

        return BoardView(
            columns=columns,
            total=total,
            type_filter=type_filter,
            include_archived=include_archived,
            archived_count=archived_count,
            view=view,
            limit_per_column=effective_limit if view != "summary" else None,
        )

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
                    self.transition_ticket(conn, schema, tid, target_status, changed_by)
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
        cur = conn.cursor()
        try:
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
            return self._link_row_to_response(row)
        except IntegrityError:
            conn.rollback()
            raise LinkAlreadyExistsError(
                f"Link already exists between ticket #{source_id} and #{data.target_id}"
            )
        except Exception:
            conn.rollback()
            raise

    def remove_link(
        self,
        conn: DBConnection,
        schema: str,
        link_id: int,
    ) -> bool:
        """Remove a ticket link.

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            link_id: Link ID.

        Returns:
            True if deleted, False if not found.
        """
        cur = conn.cursor()
        try:
            cur.execute(
                sql.SQL("DELETE FROM {}.ticket_links WHERE id = %s RETURNING id").format(
                    sql.Identifier(schema)
                ),
                (link_id,),
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
    ) -> bool:
        """Remove a ticket↔context link.

        Args:
            conn: Database connection.
            schema: PostgreSQL schema name.
            link_id: Context link ID.

        Returns:
            True if deleted, False if not found.
        """
        cur = conn.cursor()
        try:
            cur.execute(
                sql.SQL(
                    "DELETE FROM {}.ticket_context_links WHERE id = %s RETURNING id"
                ).format(sql.Identifier(schema)),
                (link_id,),
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
            SELECT tl.*, t.title as target_title, t.status as target_status
            FROM {}.ticket_links tl
            JOIN {}.tickets t ON t.id = tl.target_id
            WHERE tl.source_id IN ({ph})
            UNION ALL
            SELECT tl.*, t.title as target_title, t.status as target_status
            FROM {}.ticket_links tl
            JOIN {}.tickets t ON t.id = tl.source_id
            WHERE tl.target_id IN ({ph})
            """).format(sch, sch, sch, sch, ph=placeholders),
            ticket_ids + ticket_ids,
        )
        result: Dict[int, List[TicketLinkResponse]] = {tid: [] for tid in ticket_ids}
        for row in cur.fetchall():
            link = self._link_row_to_response(row)
            # Attribute the link to the input ticket on whichever side matches.
            if link.source_id in result:
                result[link.source_id].append(link)
            elif link.target_id in result:
                result[link.target_id].append(link)
        return result

    def _get_context_links_for_tickets_bulk(
        self, cur: Any, schema: str, ticket_ids: List[int]
    ) -> Dict[int, List[ContextLinkResponse]]:
        """Bulk-fetch ticket↔context links for many tickets in a single query."""
        if not ticket_ids:
            return {}
        sch = sql.Identifier(schema)
        placeholders = sql.SQL(", ").join(sql.Placeholder() * len(ticket_ids))
        try:
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
        except Exception:
            # Table may not exist yet on older schemas
            return {tid: [] for tid in ticket_ids}
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
            SELECT tl.*, t.title as target_title, t.status as target_status
            FROM {}.ticket_links tl
            JOIN {}.tickets t ON t.id = tl.target_id
            WHERE tl.source_id = %s
            UNION ALL
            SELECT tl.*, t.title as target_title, t.status as target_status
            FROM {}.ticket_links tl
            JOIN {}.tickets t ON t.id = tl.source_id
            WHERE tl.target_id = %s
            """).format(sch, sch, sch, sch),
            (ticket_id, ticket_id),
        )
        return [self._link_row_to_response(r) for r in cur.fetchall()]

    def _row_to_response(self, row: Dict[str, Any]) -> TicketResponse:
        """Convert a database row to a TicketResponse."""
        tags = None
        if row.get("tags"):
            try:
                tags = json.loads(row["tags"])
            except (json.JSONDecodeError, TypeError):
                tags = None

        metadata = None
        if row.get("metadata"):
            try:
                metadata = json.loads(row["metadata"])
            except (json.JSONDecodeError, TypeError):
                metadata = None

        return TicketResponse(
            id=row["id"],
            title=row["title"],
            description=row.get("description"),
            type=row["type"],
            status=row["status"],
            priority=row["priority"],
            assignee=row.get("assignee"),
            tags=tags,
            metadata=metadata,
            session_id=row.get("session_id"),
            created_by=row.get("created_by"),
            created_at=row.get("created_at"),
            updated_at=row.get("updated_at"),
            closed_at=row.get("closed_at"),
            archived_at=row.get("archived_at"),
        )

    def _link_row_to_response(self, row: Dict[str, Any]) -> TicketLinkResponse:
        """Convert a link database row to a TicketLinkResponse."""
        return TicketLinkResponse(
            id=row["id"],
            source_id=row["source_id"],
            target_id=row["target_id"],
            link_type=row["link_type"],
            created_at=row.get("created_at"),
            target_title=row.get("target_title"),
            target_status=row.get("target_status"),
        )
