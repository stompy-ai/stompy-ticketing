"""Shared ticket-list query: every count and page uses the same predicate."""

import re
from typing import Any, List

from psycopg2 import sql

from .models import TicketListFilters, TicketListResponse


def _glob_class(content: str) -> str:
    """Literal characters/ranges; descending ranges contribute no characters."""
    negate = content.startswith("!")
    if negate:
        content = content[1:]
    terms = []
    i = 0
    while i < len(content):
        if i + 2 < len(content) and content[i + 1] == "-":
            start, end = content[i], content[i + 2]
            if start <= end:
                terms.append(re.escape(start) + "-" + re.escape(end))
            i += 3
        else:
            terms.append(re.escape(content[i]))
            i += 1
    # fnmatch interprets a leading ! after discarding descending ranges too.
    if not negate and terms and terms[0].startswith("!"):
        negate = True
        terms[0] = terms[0][1:]
        if not terms[0]:
            terms.pop(0)
    if not terms:
        return "." if negate else "(?!)"
    return "[" + ("^" if negate else "") + "".join(terms) + "]"


def glob_regex(pattern: str) -> str:
    """Compile the glob language directly to PostgreSQL ARE, not Python regex.

    fnmatch remains the test oracle; its version-specific lookaheads, atomic
    groups and named backreferences never enter database queries.
    """
    parts = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        i += 1
        if char == "*":
            if not parts or parts[-1] != ".*":
                parts.append(".*")
        elif char == "?":
            parts.append(".")
        elif char == "[":
            end = i + (i < len(pattern) and pattern[i] == "!")
            if end < len(pattern) and pattern[end] == "]":
                end += 1
            end = pattern.find("]", end)
            if end == -1:
                parts.append(r"\[")
            else:
                parts.append(_glob_class(pattern[i:end]))
                i = end + 1
        else:
            parts.append(re.escape(char))
    return r"(?s)\A" + "".join(parts) + r"\Z"


def list_tickets(service, conn, schema, filters, fields):
    if filters is None:
        filters = TicketListFilters()

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
        # An assignee is an identity, not free text: 'Astra' and 'astra' are one
        # agent (STOMPY-3544). The stored spelling is kept as written. lower() is
        # case-insensitive for ASCII under the database collation.
        where_clauses.append(
            "lower(btrim(assignee, E' \\t\\r\\n')) = lower(btrim(%s, E' \\t\\r\\n'))"
        )
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

    if filters.grep:
        where_clauses.append('title COLLATE "C" ~ %s')
        params.append(glob_regex(filters.grep))

    if filters.search:
        tsquery_param = service._build_or_tsquery_param(filters.search)
        where_clauses.append("content_tsvector @@ to_tsquery('english', %s)")
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

    tickets = [service._row_to_response(r) for r in rows]
    if fields != "full":
        tickets = [service.to_card(t) for t in tickets]
    return TicketListResponse(
        tickets=tickets,
        total=total,
        limit=filters.limit,
        offset=filters.offset,
        has_more=(filters.offset + filters.limit) < total,
        by_status=by_status,
        by_type=by_type,
    )
