"""A cursor that answers board_view's queries the way Postgres would (STOMPY-3284).

The per-column rank filter is honoured from the SQL's own parameter and the
column counts come from the GROUP BY, so a board that fetches every row shows
up as rows_fetched, and a board that counts fetched rows shows up as wrong
counts. execute/fetchall/fetchone are MagicMocks wrapping the fake, so
call_args_list assertions keep working and a leftover
`cur.fetchall.return_value = ...` is inert.
"""

import itertools
import re
from unittest.mock import MagicMock

_TICKETS = re.compile(r"FROM \w+\.tickets\b")


def _is_ticket_read(text):
    """A read of the tickets table itself: not links, not the stale sweep."""
    return bool(_TICKETS.search(text)) and "closed_at <" not in text


class FakeBoardCursor:
    """Answers board_view's queries from an in-memory table."""

    def __init__(self, rows, counts=None, link_rows=None):
        self.rows = rows
        self.counts = counts  # override: what GROUP BY says when the table is "bigger"
        self.link_rows = link_rows or {}  # {"ticket_links": [...], "context_links": [...]}
        self.executed = []  # (sql_text, params)
        self.rows_fetched = 0
        self._result = []
        self._one = None
        self.execute = MagicMock(side_effect=self._execute)
        self.fetchall = MagicMock(side_effect=lambda: self._result)
        self.fetchone = MagicMock(side_effect=lambda: self._one)

    def _execute(self, query, params=None):
        from tests.test_service import _sql_to_str  # test_service imports this module

        text = _sql_to_str(query)
        params = list(params or [])
        self.executed.append((text, params))
        self._one = {"count": 0}
        self._result = []
        if "context_links" in text:
            self._result = list(self.link_rows.get("context_links", []))
        elif "ticket_links" in text:
            self._result = list(self.link_rows.get("ticket_links", []))
        elif not _is_ticket_read(text) or "archived_at IS NOT NULL" in text:
            pass  # stale sweep / archived COUNT: nothing stale, nothing archived
        elif "GROUP BY status" in text:
            counts = self.counts
            if counts is None:
                counts = {}
                for r in self.rows:
                    counts[r["status"]] = counts.get(r["status"], 0) + 1
            self._result = [{"status": s, "count": c} for s, c in sorted(counts.items())]
        elif "row_number()" in text:
            limit = params[-1] if "board_rank <= %s" in text else None
            seen, out = {}, []
            for r in self.rows:
                seen[r["status"]] = seen.get(r["status"], 0) + 1
                if limit is None or seen[r["status"]] <= limit:
                    out.append(r)
            self.rows_fetched += len(out)
            self._result = out
        else:
            # The pre-3284 shape: every matching row, limit applied in Python.
            self.rows_fetched += len(self.rows)
            self._result = list(self.rows)

    def board_queries(self):
        return [
            (t, p) for t, p in self.executed
            if _is_ticket_read(t) and "ticket_links" not in t and "context_links" not in t
        ]

    def row_query(self):
        rows = [(t, p) for t, p in self.board_queries() if "GROUP BY" not in t
                and "archived_at IS NOT NULL" not in t]
        assert len(rows) == 1, rows
        return rows[0]


_databases = itertools.count(1)


def board_conn(rows, counts=None, dsn=None, link_rows=None):
    """A connection to its OWN database unless dsn says otherwise, so the
    process-wide sweep throttle never leaks from one test into the next."""
    dsn = dsn or {"host": "h", "port": "5432", "dbname": f"d{next(_databases)}"}
    conn = MagicMock()
    cur = FakeBoardCursor(rows, counts, link_rows)
    conn.cursor.return_value = cur
    conn.get_dsn_parameters.return_value = dsn
    return conn, cur
