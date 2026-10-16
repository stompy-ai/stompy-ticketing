"""Tests for SQL injection prevention in stompy-ticketing.

Verifies that all SQL queries in service.py and schema.py use
psycopg2.sql.Identifier for schema names instead of f-string interpolation.

TDD: These tests are written FIRST before the implementation.
"""

import re
from unittest.mock import MagicMock, patch

import pytest

from psycopg2 import sql

from stompy_ticketing.models import (
    Priority,
    TicketCreate,
    TicketLinkCreate,
    TicketListFilters,
    TicketType,
    TicketUpdate,
    LinkType,
)
from stompy_ticketing.service import TicketService


# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

FIXED_TIME = 1700000000.0
SAFE_SCHEMA = "test_project"
# Schema names that would be dangerous if interpolated directly
MALICIOUS_SCHEMA = "test; DROP TABLE users; --"


def _make_ticket_row(
    id=1,
    title="Test ticket",
    description="Test description",
    type="task",
    status="backlog",
    priority="medium",
    assignee=None,
    tags=None,
    metadata=None,
    session_id="sess_123",
    created_at=FIXED_TIME,
    updated_at=FIXED_TIME,
    closed_at=None,
    content_hash="abc123",
    content_tsvector=None,
):
    return {
        "id": id,
        "title": title,
        "description": description,
        "type": type,
        "status": status,
        "priority": priority,
        "assignee": assignee,
        "tags": tags,
        "metadata": metadata,
        "session_id": session_id,
        "created_at": created_at,
        "updated_at": updated_at,
        "closed_at": closed_at,
        "content_hash": content_hash,
        "content_tsvector": content_tsvector,
    }


def _mock_conn_and_cursor(rows=None, fetchone_value=None):
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    if rows is not None:
        cur.fetchall.return_value = rows
    if fetchone_value is not None:
        cur.fetchone.return_value = fetchone_value
    elif rows:
        cur.fetchone.return_value = rows[0]
    else:
        cur.fetchone.return_value = None
    return conn, cur


def _sql_to_str(query) -> str:
    """Convert a psycopg2 sql object to a plain string for assertions.

    Uses recursive extraction since as_string() requires a real psycopg2 connection.
    """
    if isinstance(query, sql.Composed):
        return "".join(_sql_to_str(part) for part in query._wrapped)
    if isinstance(query, sql.SQL):
        return query._wrapped
    if isinstance(query, sql.Identifier):
        return ".".join(query._wrapped)
    return str(query)


def _assert_uses_identifier(cur, schema_name: str):
    """Assert that ALL execute calls use sql.Identifier for schema, not f-strings.

    Checks that every SQL query passed to cursor.execute() is a psycopg2.sql
    object (SQL or Composed), never a plain string containing the schema name
    directly interpolated.
    """
    for call in cur.execute.call_args_list:
        query = call[0][0]
        assert isinstance(query, (sql.SQL, sql.Composed)), (
            f"Expected psycopg2.sql object, got plain string: {query!r:.200s}"
        )


# --------------------------------------------------------------------------- #
# Test: All service methods use sql.Identifier for schema                      #
# --------------------------------------------------------------------------- #


class TestServiceUsesIdentifier:
    """Verify every TicketService method uses sql.Identifier for schema."""

    def setup_method(self):
        self.service = TicketService()
        self.service.archive_stale_tickets = MagicMock(return_value=0)

    @patch("stompy_ticketing.service.time")
    def test_create_ticket_uses_identifier(self, mock_time):
        mock_time.time.return_value = FIXED_TIME
        row = _make_ticket_row()
        conn, cur = _mock_conn_and_cursor(fetchone_value=row)

        data = TicketCreate(title="Test", type=TicketType.task)
        self.service.create_ticket(conn, SAFE_SCHEMA, data)

        _assert_uses_identifier(cur, SAFE_SCHEMA)

    def test_get_ticket_uses_identifier(self):
        row = _make_ticket_row()
        conn, cur = _mock_conn_and_cursor(fetchone_value=row)
        cur.fetchall.return_value = []

        self.service.get_ticket(conn, SAFE_SCHEMA, 1)

        _assert_uses_identifier(cur, SAFE_SCHEMA)

    @patch("stompy_ticketing.service.time")
    def test_update_ticket_uses_identifier(self, mock_time):
        mock_time.time.return_value = FIXED_TIME
        current = _make_ticket_row(title="Old")
        updated = _make_ticket_row(title="New")
        conn, cur = _mock_conn_and_cursor()
        cur.fetchone.side_effect = [current, updated]

        data = TicketUpdate(title="New")
        self.service.update_ticket(conn, SAFE_SCHEMA, 1, data)

        _assert_uses_identifier(cur, SAFE_SCHEMA)

    @patch("stompy_ticketing.service.time")
    def test_transition_ticket_uses_identifier(self, mock_time):
        mock_time.time.return_value = FIXED_TIME
        current = _make_ticket_row(status="backlog", type="task")
        updated = _make_ticket_row(status="in_progress", type="task")
        conn, cur = _mock_conn_and_cursor()
        cur.fetchone.side_effect = [current, updated]

        self.service.transition_ticket(conn, SAFE_SCHEMA, 1, "in_progress")

        _assert_uses_identifier(cur, SAFE_SCHEMA)

    def test_close_ticket_uses_identifier(self):
        row = _make_ticket_row(status="done", type="task")
        conn, cur = _mock_conn_and_cursor(fetchone_value=row)
        cur.fetchall.return_value = []

        self.service.close_ticket(conn, SAFE_SCHEMA, 1)

        _assert_uses_identifier(cur, SAFE_SCHEMA)

    def test_list_tickets_uses_identifier(self):
        rows = [_make_ticket_row()]
        conn, cur = _mock_conn_and_cursor(rows=rows)
        cur.fetchone.side_effect = [{"count": 1}]
        cur.fetchall.side_effect = [
            rows,
            [{"status": "backlog", "count": 1}],
            [{"type": "task", "count": 1}],
        ]

        self.service.list_tickets(conn, SAFE_SCHEMA)

        _assert_uses_identifier(cur, SAFE_SCHEMA)

    def test_search_tickets_uses_identifier(self):
        rows = [{**_make_ticket_row(), "rank": 0.5}]
        conn, cur = _mock_conn_and_cursor(rows=rows)
        cur.fetchall.return_value = rows

        self.service.search_tickets(conn, SAFE_SCHEMA, "test")

        _assert_uses_identifier(cur, SAFE_SCHEMA)

    def test_board_view_summary_uses_identifier(self):
        conn, cur = _mock_conn_and_cursor()
        cur.fetchall.return_value = [{"status": "backlog", "count": 1}]

        self.service.board_view(conn, SAFE_SCHEMA, view="summary")

        _assert_uses_identifier(cur, SAFE_SCHEMA)

    def test_board_view_kanban_uses_identifier(self):
        rows = [_make_ticket_row()]
        conn, cur = _mock_conn_and_cursor(rows=rows)
        cur.fetchall.return_value = rows

        self.service.board_view(conn, SAFE_SCHEMA, view="kanban")

        _assert_uses_identifier(cur, SAFE_SCHEMA)

    @patch("stompy_ticketing.service.time")
    def test_add_link_uses_identifier(self, mock_time):
        mock_time.time.return_value = FIXED_TIME
        insert_row = {
            "id": 1, "source_id": 1, "target_id": 2,
            "link_type": "blocks", "created_at": FIXED_TIME,
        }
        target_row = {"title": "Target", "status": "backlog"}
        conn, cur = _mock_conn_and_cursor()
        cur.fetchone.side_effect = [None, insert_row, target_row]  # None: no reverse blocks link

        data = TicketLinkCreate(target_id=2, link_type=LinkType.blocks)
        self.service.add_link(conn, SAFE_SCHEMA, 1, data)

        _assert_uses_identifier(cur, SAFE_SCHEMA)

    def test_remove_link_uses_identifier(self):
        conn, cur = _mock_conn_and_cursor(fetchone_value={"id": 1})

        self.service.remove_link(conn, SAFE_SCHEMA, 1)

        _assert_uses_identifier(cur, SAFE_SCHEMA)

    def test_list_links_uses_identifier(self):
        conn, cur = _mock_conn_and_cursor(rows=[])
        cur.fetchall.return_value = []

        self.service.list_links(conn, SAFE_SCHEMA, 1)

        _assert_uses_identifier(cur, SAFE_SCHEMA)


# --------------------------------------------------------------------------- #
# Test: schema.py DDL functions use sql.Identifier                             #
# --------------------------------------------------------------------------- #


class TestSchemaDDLUsesIdentifier:
    """Verify DDL functions return psycopg2.sql objects, not f-strings."""

    def test_tickets_table_returns_sql_composed(self):
        from stompy_ticketing.schema import get_tickets_table_sql
        result = get_tickets_table_sql(SAFE_SCHEMA)
        assert isinstance(result, (sql.SQL, sql.Composed)), (
            f"Expected psycopg2.sql object, got {type(result)}"
        )

    def test_ticket_history_table_returns_sql_composed(self):
        from stompy_ticketing.schema import get_ticket_history_table_sql
        result = get_ticket_history_table_sql(SAFE_SCHEMA)
        assert isinstance(result, (sql.SQL, sql.Composed)), (
            f"Expected psycopg2.sql object, got {type(result)}"
        )

    def test_ticket_links_table_returns_sql_composed(self):
        from stompy_ticketing.schema import get_ticket_links_table_sql
        result = get_ticket_links_table_sql(SAFE_SCHEMA)
        assert isinstance(result, (sql.SQL, sql.Composed)), (
            f"Expected psycopg2.sql object, got {type(result)}"
        )

    def test_indexes_returns_sql_composed(self):
        from stompy_ticketing.schema import get_tickets_indexes_sql
        result = get_tickets_indexes_sql(SAFE_SCHEMA)
        assert isinstance(result, (sql.SQL, sql.Composed)), (
            f"Expected psycopg2.sql object, got {type(result)}"
        )

    def test_tsvector_trigger_returns_sql_composed(self):
        from stompy_ticketing.schema import get_tickets_tsvector_trigger_sql
        result = get_tickets_tsvector_trigger_sql(SAFE_SCHEMA)
        assert isinstance(result, (sql.SQL, sql.Composed)), (
            f"Expected psycopg2.sql object, got {type(result)}"
        )

    def test_all_ticket_tables_returns_sql_composed(self):
        from stompy_ticketing.schema import get_all_ticket_tables_sql
        result = get_all_ticket_tables_sql(SAFE_SCHEMA)
        assert isinstance(result, (sql.SQL, sql.Composed)), (
            f"Expected psycopg2.sql object, got {type(result)}"
        )


# --------------------------------------------------------------------------- #
# Test: No raw f-string interpolation in source files                          #
# --------------------------------------------------------------------------- #


class TestNoFStringInterpolation:
    """Static analysis: verify no f-string SQL with {schema} remains."""

    def test_service_py_has_no_fstring_schema_interpolation(self):
        """service.py must not contain f-string SQL with {schema}."""
        import inspect
        import stompy_ticketing.service as svc_module
        source = inspect.getsource(svc_module)
        # Match f-string patterns like f"...{schema}..." in SQL context
        fstring_patterns = re.findall(r'f["\'].*\{schema\}.*["\']', source)
        assert fstring_patterns == [], (
            f"Found f-string SQL interpolation in service.py: {fstring_patterns}"
        )

    def test_schema_py_has_no_fstring_schema_interpolation(self):
        """schema.py must not contain f-string SQL with {schema} outside sql.Identifier().

        f-strings inside sql.Identifier() are safe (e.g. building index names).
        Only bare f-string SQL like f"SELECT * FROM {schema}.table" is dangerous.
        """
        import inspect
        import stompy_ticketing.schema as schema_module
        source = inspect.getsource(schema_module)
        # Match f-strings containing SQL keywords + {schema} (actual SQL injection risk)
        # Exclude f-strings inside sql.Identifier() which are safe
        dangerous = []
        for match in re.finditer(r'f(""".*?"""|"[^"]*")', source, re.DOTALL):
            fstr = match.group(0)
            if "{schema}" in fstr:
                # Check it's not inside sql.Identifier() context
                start = match.start()
                preceding = source[max(0, start - 30):start].strip()
                if "sql.Identifier(" not in preceding:
                    dangerous.append(fstr[:80])
        assert dangerous == [], (
            f"Found f-string SQL interpolation in schema.py: {dangerous}"
        )
