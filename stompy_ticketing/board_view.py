"""The ticket board read, bounded in SQL (STOMPY-3284).

The per-column limit is a row_number() window, the column counts come from
one GROUP BY, and the row query names its columns. fields="card" (the agent
doors) ships description_preview and no body; fields="full" (the host's web
door, whose detail dialog renders the board row, STOMPY-1519) ships the body
whole. The legacy stale-archive sweep runs through stale_archive.sweep_if_due,
the throttle the board shares with ticket list and search.

Card links (also 3284): every full link row repeats source, target AND peer
title, and a context-link row repeats the card's own title, so on a real
board links outweighed everything else. A card carries CardLinks and context
labels instead, at most BOARD_CARD_LINK_CAP of each, blockers first and open
peers before closed ones, with links_total / context_links_total set when
capped. The full rows stay one ticket(get) or ticket_link(list) away.

Named columns tolerate an older table (also 3284): created_by and the lease
columns are host deltas, and a schema that predates them has no such column,
where SELECT * never cared. The row query reads the table's real columns once
per (database, schema) and selects an absent one as NULL.

No swallowed statement (STOMPY-3296): on Postgres a failed statement aborts
the transaction, so `except: pass` around a read answered and left the
caller's connection failing its NEXT statement. The one absence tolerated,
a schema predating ticket_context_links (migration 32), is asked of the
catalog per (database, schema) and the read skipped there; any other failure
raises its own error to the door's catch-all.
"""

import time
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

from psycopg2 import sql

from stompy_ticketing.models import BoardColumn, BoardView, CardLink, CompactTicket
from stompy_ticketing.stale_archive import SWEEP_INTERVAL_SECONDS, _database, sweep_if_due

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
# minimal: an unlocked dict, as in stale_archive; a race costs one extra
# catalog read. A complete table is cached for good (columns are only ever
# added); an incomplete one is re-read after SWEEP_INTERVAL_SECONDS so a
# migration that lands mid-process is picked up.
_table_columns: Dict[Tuple[Any, str], Tuple[FrozenSet[str], Optional[float]]] = {}


def _ticket_columns(conn, cur, schema: str) -> Optional[FrozenSet[str]]:
    """The tickets table's actual columns, or None when they cannot be read
    (then every column is named, as before)."""
    database = _database(conn)
    key, now = (database, schema), time.monotonic()
    hit = _table_columns.get(key) if database is not None else None
    if hit and (hit[1] is None or now < hit[1]):
        return hit[0]
    cur.execute(
        sql.SQL(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s"
        ),
        (schema, "tickets"),
    )
    found = frozenset(r["column_name"] for r in cur.fetchall())
    if not found:
        return None
    if database is not None:
        complete = found.issuperset(CARD_COLUMNS)
        _table_columns[key] = (found, None if complete else now + SWEEP_INTERVAL_SECONDS)
    return found


#: (database, schema) -> (ticket_context_links exists, re-read deadline or None).
_context_links_present: Dict[Tuple[Any, str], Tuple[bool, Optional[float]]] = {}


def context_links_present(conn, cur, schema: str) -> bool:
    """Whether the schema has ticket_context_links (migration 32). Present is
    cached for good; absent is re-read after SWEEP_INTERVAL_SECONDS, like an
    incomplete tickets table. A catalog that does not answer True/False
    counts as present: the read then runs, as before."""
    database = _database(conn)
    key, now = (database, schema), time.monotonic()
    hit = _context_links_present.get(key) if database is not None else None
    if hit and (hit[1] is None or now < hit[1]):
        return hit[0]
    cur.execute(
        sql.SQL(
            "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_tables "
            "WHERE schemaname = %s AND tablename = %s) AS present"
        ),
        (schema, "ticket_context_links"),
    )
    row = cur.fetchone()
    found = row.get("present") if isinstance(row, dict) else None
    if found is not True and found is not False:
        return True
    if database is not None:
        _context_links_present[key] = (found, None if found else now + SWEEP_INTERVAL_SECONDS)
    return found


def _column(name: str, present: Optional[FrozenSet[str]]) -> sql.Composable:
    if present is None or name in present:
        return sql.Identifier(name)
    return sql.SQL("NULL AS {}").format(sql.Identifier(name))


#: Card link order: what blocks this card, what it blocks, then structure.
_RELATION_RANK = {"blocked_by": 0, "blocks": 1, "parent": 2, "duplicate": 3, "related": 4}


def _card_links(ticket, cap: int, closed) -> None:
    """Replace a card's full link rows with CardLinks and labels, capped."""
    links = sorted(
        ticket.links,
        key=lambda l: (_RELATION_RANK.get(l.relation, len(_RELATION_RANK)),
                       l.peer_status in closed, -l.peer_id),
    )
    ticket.links = [
        CardLink(peer_id=l.peer_id, relation=l.relation, direction=l.direction,
                 peer_status=l.peer_status)
        for l in links[:cap]
    ]
    if len(links) > cap:
        ticket.links_total = len(links)
    labels = list(dict.fromkeys(c.context_label for c in ticket.context_links))
    ticket.context_links = labels[:cap]
    if len(labels) > cap:
        ticket.context_links_total = len(labels)


class BoardMixin:
    #: Links of each kind on one board card; a hub ticket carries hundreds.
    BOARD_CARD_LINK_CAP = 20

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

        sweep_if_due(self, conn, schema)

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

        cur.execute(
            sql.SQL(
                "SELECT COUNT(*) as count FROM {} WHERE archived_at IS NOT NULL"
            ).format(table),
        )
        row = cur.fetchone()  # COUNT(*) always has a row on Postgres; a stub may not
        archived_count = row["count"] if row else 0

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
        # Compact names only columns every tickets table has had.
        present = None if is_compact else _ticket_columns(conn, cur, schema)
        inner = [_column(c, present) for c in names]
        outer = [sql.Identifier(c) for c in names]
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
                link_map = self._get_links_for_tickets_bulk(cur, schema, visible_ids)
                ctx_link_map = (
                    self._get_context_links_for_tickets_bulk(cur, schema, visible_ids)
                    if context_links_present(conn, cur, schema) else {}
                )
                closed = set(get_hidden_statuses(None))
                for col in columns:
                    for t in col.tickets:
                        t.links = link_map.get(t.id, [])
                        t.context_links = ctx_link_map.get(t.id, [])
                        if fields == "card":
                            _card_links(t, self.BOARD_CARD_LINK_CAP, closed)

        return BoardView(
            columns=columns,
            total=sum(c.count for c in columns),
            type_filter=type_filter,
            include_archived=include_archived,
            archived_count=archived_count,
            view=view,
            limit_per_column=effective_limit,
        )
