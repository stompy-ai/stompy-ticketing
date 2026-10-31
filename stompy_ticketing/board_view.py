"""The ticket board read, bounded in SQL (STOMPY-3284).

The per-column limit is a row_number() window, the column counts come from
one GROUP BY, and the row query names its columns. fields="card" (the agent
doors) ships description_preview and no body; fields="full" (the host's web
door, whose detail dialog renders the board row, STOMPY-1519) ships the body
whole. The legacy stale-archive sweep still runs from the board, at most once
per database and schema per process per SWEEP_INTERVAL_SECONDS.
"""

import time
from typing import Any, Dict, List, Optional, Tuple

from psycopg2 import sql

from stompy_ticketing.models import BoardColumn, BoardView, CompactTicket

#: The board's stale sweep runs at most once per (database, schema) per this window.
SWEEP_INTERVAL_SECONDS = 600

_clock = time.monotonic
# minimal: an unlocked dict; a race between two threads costs one extra sweep,
# which is the pre-3284 behaviour. Bounded by the number of (database, schema) pairs.
_last_sweep: Dict[Tuple[Any, str], float] = {}

#: What row_to_response + the preview read (ticket_projection.py, leases.py).
CARD_COLUMNS = (
    "id", "title", "type", "status", "priority", "assignee", "tags", "metadata",
    "session_id", "created_by", "created_at", "updated_at", "closed_at",
    "archived_at", "claimed_by", "claimed_at", "claimed_until",
)
COMPACT_COLUMNS = ("id", "title", "type", "status", "priority", "assignee")

_PRIORITY_RANK = sql.SQL(
    "CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1 WHEN 'medium' THEN 2 "
    "WHEN 'low' THEN 3 ELSE 4 END"
)


def _database(conn) -> Optional[Tuple[Any, Any, Any]]:
    """Which database this connection is on: tenants have their own Neon
    databases with same-named schemas, so a schema alone is not a key."""
    try:
        params = conn.get_dsn_parameters()
        return (params.get("host"), params.get("port"), params.get("dbname"))
    except Exception:
        return None


def _sweep_due(conn, schema: str) -> bool:
    database = _database(conn)
    if database is None:
        return True  # unknown database: never let a throttle skip a tenant's sweep
    key, now = (database, schema), _clock()
    last = _last_sweep.get(key)
    if last is not None and now - last < SWEEP_INTERVAL_SECONDS:
        return False
    _last_sweep[key] = now  # an attempt counts: a failing sweep must not retry every read
    return True


class BoardMixin:
    def board_view(
        self,
        conn,
        schema: str,
        type_filter: Optional[str] = None,
        view: str = "kanban",
        status_filter: Optional[str] = None,
        include_terminal: bool = False,
        include_archived: bool = False,
        limit: Optional[int] = None,
        fields: str = "full",
    ) -> BoardView:
        """Get a board view of tickets grouped by status.

        Args:
            view: "kanban" (tickets), "summary" (counts only), "compact"
                  (id/title/type/status/priority/assignee), or "detail"
                  (alias for kanban).
            limit: Max tickets per column (default 10 for kanban/compact).
                   Use 0 for unlimited.
            fields: "card" — kanban tickets carry description_preview and
                    description=None (the agent doors; the body is one
                    ticket(get) away). "full" — description travels whole
                    beside the preview (the host's web board, STOMPY-1519).
        """
        from stompy_ticketing.service import STATE_MACHINES, get_hidden_statuses

        if fields not in ("card", "full"):
            raise ValueError(f"fields must be 'card' or 'full', not {fields!r}")

        if _sweep_due(conn, schema):
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
            # Terminal statuses AND parked (STOMPY-1746) are not queue.
            hidden = get_hidden_statuses(type_filter)
            conditions.append(f"status NOT IN ({', '.join(['%s'] * len(hidden))})")
            params.extend(hidden)
        where = sql.SQL(f"WHERE {' AND '.join(conditions)}" if conditions else "")
        table = sql.SQL("{}.tickets").format(sql.Identifier(schema))

        archived_count = 0
        try:
            cur.execute(
                sql.SQL(
                    "SELECT COUNT(*) as count FROM {} WHERE archived_at IS NOT NULL"
                ).format(table),
            )
            archived_count = cur.fetchone()["count"]
        except Exception:
            pass

        cur.execute(
            sql.SQL(
                "SELECT status, COUNT(*) as count FROM {} {} GROUP BY status ORDER BY status"
            ).format(table, where),
            params,
        )
        counts = {r["status"]: r["count"] for r in cur.fetchall()}

        if view == "summary":
            return BoardView(
                columns=[BoardColumn(status=s, count=c, tickets=[]) for s, c in counts.items()],
                total=sum(counts.values()),
                type_filter=type_filter,
                include_archived=include_archived,
                archived_count=archived_count,
                view=view,
                limit_per_column=None,
            )

        is_compact = view == "compact"
        if limit is not None:
            effective_limit = None if limit == 0 else max(1, limit)
        else:
            effective_limit = self.BOARD_DEFAULT_LIMIT

        names = COMPACT_COLUMNS if is_compact else CARD_COLUMNS
        inner = [sql.Identifier(c) for c in names]
        outer = list(inner)
        select_params: List[Any] = []
        if not is_compact:
            outer.append(sql.Identifier("description"))
            if fields == "card":
                # One char past the cutoff is enough for _attach_card_preview
                # to decide exactly as it would on the whole text.
                inner.append(sql.SQL("LEFT(description, %s) AS description"))
                select_params.append(
                    self.BOARD_DESC_MAX_LENGTH + len(self.BOARD_DESC_ELLIPSIS) + 1
                )
            else:
                inner.append(sql.Identifier("description"))
        rank_filter = sql.SQL("WHERE board_rank <= %s" if effective_limit else "")
        cur.execute(
            sql.SQL(
                "SELECT {outer} FROM ("
                "SELECT {inner}, row_number() OVER (PARTITION BY status "
                "ORDER BY {rank}, updated_at DESC, id DESC) AS board_rank "
                "FROM {table} {where}) ranked {rank_filter} ORDER BY status, board_rank"
            ).format(
                outer=sql.SQL(", ").join(outer),
                inner=sql.SQL(", ").join(inner),
                rank=_PRIORITY_RANK,
                table=table,
                where=where,
                rank_filter=rank_filter,
            ),
            select_params + params + ([effective_limit] if effective_limit else []),
        )
        groups: Dict[str, List[Dict[str, Any]]] = {}
        for r in cur.fetchall():
            groups.setdefault(r["status"], []).append(r)

        present = sorted(set(counts) | set(groups))
        if type_filter and type_filter in STATE_MACHINES:
            ordered = list(STATE_MACHINES[type_filter]["transitions"].keys())
        else:
            ordered = []
        ordered += [s for s in present if s not in ordered]

        columns = []
        for status in ordered:
            visible = groups.get(status, [])
            # Two statements, no snapshot: a row landing between them must not
            # make truncated_count negative. count == len(tickets) + truncated_count.
            count = max(counts.get(status, 0), len(visible))
            column = BoardColumn(
                status=status,
                count=count,
                has_more=count > len(visible),
                truncated_count=count - len(visible),
            )
            if is_compact:
                column.compact_tickets = [
                    CompactTicket(**{c: r.get(c) for c in COMPACT_COLUMNS}) for r in visible
                ]
            else:
                for r in visible:
                    ticket = self._row_to_response(r)
                    self._attach_card_preview(ticket)
                    if fields == "card":
                        ticket.description = None
                    column.tickets.append(ticket)
            columns.append(column)

        # Bug #173: links on kanban cards, one bulk query per kind (no N+1).
        if not is_compact:
            visible_ids = [t.id for col in columns for t in col.tickets]
            if visible_ids:
                try:
                    link_map = self._get_links_for_tickets_bulk(cur, schema, visible_ids)
                    ctx_link_map = self._get_context_links_for_tickets_bulk(
                        cur, schema, visible_ids
                    )
                except Exception:
                    link_map, ctx_link_map = {}, {}
                for col in columns:
                    for t in col.tickets:
                        t.links = link_map.get(t.id, [])
                        t.context_links = ctx_link_map.get(t.id, [])

        return BoardView(
            columns=columns,
            total=sum(c.count for c in columns),
            type_filter=type_filter,
            include_archived=include_archived,
            archived_count=archived_count,
            view=view,
            limit_per_column=effective_limit,
        )
