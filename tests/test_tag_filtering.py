"""Tests for tag filtering in list_tickets.

TDD: Tag filtering allows querying tickets by tag (match ANY of the given tags).
Tags are stored as JSON arrays in a TEXT column, so we use SQL LIKE on the
serialized string.
"""

import json
from unittest.mock import MagicMock, patch

import pytest
from psycopg2 import sql as psql

from stompy_ticketing.models import TicketListFilters, TicketType, Priority
from stompy_ticketing.service import TicketService


# --------------------------------------------------------------------------- #
# Constants & helpers                                                          #
# --------------------------------------------------------------------------- #

FIXED_TIME = 1700000000.0
SCHEMA = "test_project"


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
    archived_at=None,
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
        "archived_at": archived_at,
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
    if isinstance(query, psql.Composed):
        parts = []
        for part in query._wrapped:
            parts.append(_sql_to_str(part))
        return "".join(parts)
    if isinstance(query, psql.SQL):
        return query._wrapped
    if isinstance(query, psql.Identifier):
        return ".".join(query._wrapped)
    return str(query)


# --------------------------------------------------------------------------- #
# Model tests                                                                  #
# --------------------------------------------------------------------------- #


class TestTicketListFiltersTags:
    """Test that TicketListFilters accepts a tags field."""

    def test_tags_field_exists_and_defaults_to_none(self):
        filters = TicketListFilters()
        assert filters.tags is None

    def test_tags_field_accepts_string(self):
        filters = TicketListFilters(tags="worker-alert,needs-review")
        assert filters.tags == "worker-alert,needs-review"

    def test_tags_field_accepts_single_tag(self):
        filters = TicketListFilters(tags="needs-review")
        assert filters.tags == "needs-review"


# --------------------------------------------------------------------------- #
# Service tests                                                                #
# --------------------------------------------------------------------------- #


@pytest.mark.usefixtures("known_ticket_card_columns")
class TestListTicketsTagFiltering:
    """Test that list_tickets generates correct SQL for tag filtering."""

    def setup_method(self):
        self.service = TicketService()
        # Mock the lazy archive trigger
        self.service.archive_stale_tickets = MagicMock(return_value=0)

    def _setup_list_mocks(self, cur, rows):
        """Configure cursor mocks for list_tickets (4 queries: main, count, by_status, by_type)."""
        cur.fetchall.side_effect = [
            rows,  # main query
            [{"status": "triage", "count": len(rows)}] if rows else [],  # by_status
            [{"type": "bug", "count": len(rows)}] if rows else [],  # by_type
        ]
        cur.fetchone.return_value = {"count": len(rows)}  # count query

    def test_single_tag_filter_generates_like_clause(self):
        """Filtering by a single tag should produce a LIKE clause."""
        rows = [_make_ticket_row(tags=json.dumps(["worker-alert", "needs-review"]))]
        conn, cur = _mock_conn_and_cursor()
        self._setup_list_mocks(cur, rows)

        filters = TicketListFilters(tags="needs-review")
        result = self.service.list_tickets(conn, SCHEMA, filters)

        call_args = cur.execute.call_args_list[0]
        query_str = _sql_to_str(call_args[0][0])
        params = call_args[0][1]

        assert "tags LIKE" in query_str
        assert "%needs-review%" in params

    def test_multiple_tags_filter_generates_or_clauses(self):
        """Filtering by comma-separated tags should produce OR'd LIKE clauses."""
        rows = [_make_ticket_row(tags=json.dumps(["worker-alert"]))]
        conn, cur = _mock_conn_and_cursor()
        self._setup_list_mocks(cur, rows)

        filters = TicketListFilters(tags="worker-alert,needs-review")
        result = self.service.list_tickets(conn, SCHEMA, filters)

        call_args = cur.execute.call_args_list[0]
        query_str = _sql_to_str(call_args[0][0])
        params = call_args[0][1]

        assert "tags LIKE" in query_str
        assert " OR " in query_str
        assert "%worker-alert%" in params
        assert "%needs-review%" in params

    def test_no_tags_filter_no_like_clause(self):
        """When tags is None, no LIKE clause should be generated."""
        rows = [_make_ticket_row()]
        conn, cur = _mock_conn_and_cursor()
        self._setup_list_mocks(cur, rows)

        filters = TicketListFilters(tags=None)
        result = self.service.list_tickets(conn, SCHEMA, filters)

        call_args = cur.execute.call_args_list[0]
        query_str = _sql_to_str(call_args[0][0])

        assert "tags LIKE" not in query_str

    def test_tags_combined_with_other_filters(self):
        """Tag filtering should AND with other filters like type and status."""
        rows = [_make_ticket_row(type="bug", status="triage",
                                 tags=json.dumps(["worker-alert"]))]
        conn, cur = _mock_conn_and_cursor()
        self._setup_list_mocks(cur, rows)

        filters = TicketListFilters(
            tags="worker-alert",
            type=TicketType.bug,
            status="triage",
        )
        result = self.service.list_tickets(conn, SCHEMA, filters)

        call_args = cur.execute.call_args_list[0]
        query_str = _sql_to_str(call_args[0][0])
        params = call_args[0][1]

        assert "type = %s" in query_str
        assert "status = %s" in query_str
        assert "tags LIKE" in query_str
        assert "bug" in params
        assert "triage" in params
        assert "%worker-alert%" in params

    def test_whitespace_in_tags_is_trimmed(self):
        """Tags with whitespace around commas should be trimmed."""
        conn, cur = _mock_conn_and_cursor()
        self._setup_list_mocks(cur, [])

        filters = TicketListFilters(tags=" worker-alert , needs-review ")
        result = self.service.list_tickets(conn, SCHEMA, filters)

        call_args = cur.execute.call_args_list[0]
        params = call_args[0][1]

        assert "%worker-alert%" in params
        assert "%needs-review%" in params
        assert "% worker-alert %" not in params


# --------------------------------------------------------------------------- #
# MCP tool integration                                                         #
# --------------------------------------------------------------------------- #


class TestMCPToolTagsParam:
    """Test that the ticket MCP tool passes tags to list filters."""

    def test_tags_param_passed_to_filters(self):
        """When tags param is provided for list action, it should be in filters."""
        # This tests the model level — TicketListFilters accepts tags
        filters = TicketListFilters(
            tags="needs-review",
            status="triage",
            type=TicketType.bug,
        )
        assert filters.tags == "needs-review"
        assert filters.status == "triage"
        assert filters.type == TicketType.bug


# --------------------------------------------------------------------------- #
# list_tags service tests                                                      #
# --------------------------------------------------------------------------- #


class TestListTags:
    """Test the list_tags service method for tag discovery."""

    def setup_method(self):
        self.service = TicketService()

    def test_list_tags_returns_unique_tags_with_counts(self):
        """list_tags should return each unique tag with its usage count."""
        rows = [
            {"tag": "worker-alert", "count": 5},
            {"tag": "needs-review", "count": 3},
            {"tag": "guardian", "count": 1},
        ]
        conn, cur = _mock_conn_and_cursor(rows=rows)

        result = self.service.list_tags(conn, SCHEMA)

        assert len(result) == 3
        assert result[0] == {"tag": "worker-alert", "count": 5}
        assert result[1] == {"tag": "needs-review", "count": 3}
        assert result[2] == {"tag": "guardian", "count": 1}

    def test_list_tags_empty_when_no_tags(self):
        """list_tags should return empty list when no tickets have tags."""
        conn, cur = _mock_conn_and_cursor(rows=[])

        result = self.service.list_tags(conn, SCHEMA)

        assert result == []

    def test_list_tags_excludes_archived_by_default(self):
        """By default, list_tags should filter out archived tickets."""
        conn, cur = _mock_conn_and_cursor(rows=[])

        self.service.list_tags(conn, SCHEMA, include_archived=False)

        call_args = cur.execute.call_args_list[0]
        query_str = _sql_to_str(call_args[0][0])
        assert "archived_at IS NULL" in query_str

    def test_list_tags_includes_archived_when_requested(self):
        """When include_archived=True, no archived_at filter should be present."""
        conn, cur = _mock_conn_and_cursor(rows=[])

        self.service.list_tags(conn, SCHEMA, include_archived=True)

        call_args = cur.execute.call_args_list[0]
        query_str = _sql_to_str(call_args[0][0])
        assert "archived_at" not in query_str

    def test_list_tags_sql_uses_jsonb_unnest(self):
        """The SQL should use jsonb_array_elements_text to unnest tags."""
        conn, cur = _mock_conn_and_cursor(rows=[])

        self.service.list_tags(conn, SCHEMA)

        call_args = cur.execute.call_args_list[0]
        query_str = _sql_to_str(call_args[0][0])
        assert "jsonb_array_elements_text" in query_str
        assert "tags::jsonb" in query_str
        assert "GROUP BY tag" in query_str
        assert "ORDER BY count DESC" in query_str
