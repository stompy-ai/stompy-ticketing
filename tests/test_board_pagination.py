"""Tests for board view pagination and compact mode (Bug #151).

Verifies that ticket_board respects per-column limits and supports
compact view mode to keep MCP responses under size limits.
"""

from unittest.mock import MagicMock

from stompy_ticketing.service import TicketService

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


def _mock_conn_and_cursor(rows=None):
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    if rows is not None:
        cur.fetchall.return_value = rows
    else:
        cur.fetchall.return_value = []
    # archived_count query
    cur.fetchone.return_value = {"count": 0}
    return conn, cur


class TestBoardPaginationLimit:
    """board_view should respect per-column limit."""

    def setup_method(self):
        self.service = TicketService()
        # Disable lazy archival trigger
        self.service.archive_stale_tickets = MagicMock(return_value=0)

    def test_default_limit_caps_at_10(self):
        """Default limit is 10 tickets per column."""
        rows = [
            _make_ticket_row(id=i, status="backlog") for i in range(25)
        ]
        conn, cur = _mock_conn_and_cursor(rows)

        result = self.service.board_view(conn, SCHEMA, view="kanban")

        backlog_col = next(c for c in result.columns if c.status == "backlog")
        assert backlog_col.count == 25  # Total count preserved
        assert len(backlog_col.tickets) == 10  # Only 10 returned
        assert backlog_col.has_more is True

    def test_explicit_limit(self):
        """Explicit limit=5 returns at most 5 per column."""
        rows = [
            _make_ticket_row(id=i, status="backlog") for i in range(15)
        ]
        conn, cur = _mock_conn_and_cursor(rows)

        result = self.service.board_view(conn, SCHEMA, view="kanban", limit=5)

        backlog_col = next(c for c in result.columns if c.status == "backlog")
        assert backlog_col.count == 15
        assert len(backlog_col.tickets) == 5
        assert backlog_col.has_more is True
        assert result.limit_per_column == 5

    def test_limit_zero_returns_all(self):
        """limit=0 disables pagination — returns all tickets."""
        rows = [
            _make_ticket_row(id=i, status="backlog") for i in range(30)
        ]
        conn, cur = _mock_conn_and_cursor(rows)

        result = self.service.board_view(conn, SCHEMA, view="kanban", limit=0)

        backlog_col = next(c for c in result.columns if c.status == "backlog")
        assert backlog_col.count == 30
        assert len(backlog_col.tickets) == 30
        assert backlog_col.has_more is False

    def test_has_more_false_when_under_limit(self):
        """has_more is False when column has fewer tickets than limit."""
        rows = [
            _make_ticket_row(id=i, status="backlog") for i in range(3)
        ]
        conn, cur = _mock_conn_and_cursor(rows)

        result = self.service.board_view(conn, SCHEMA, view="kanban", limit=10)

        backlog_col = next(c for c in result.columns if c.status == "backlog")
        assert backlog_col.count == 3
        assert len(backlog_col.tickets) == 3
        assert backlog_col.has_more is False

    def test_limit_applies_per_column(self):
        """Each column is independently limited."""
        rows = [
            _make_ticket_row(id=i, status="backlog") for i in range(8)
        ] + [
            _make_ticket_row(id=i + 100, status="in_progress") for i in range(12)
        ]
        conn, cur = _mock_conn_and_cursor(rows)

        result = self.service.board_view(conn, SCHEMA, view="kanban", limit=5)

        backlog_col = next(c for c in result.columns if c.status == "backlog")
        progress_col = next(c for c in result.columns if c.status == "in_progress")

        assert backlog_col.count == 8
        assert len(backlog_col.tickets) == 5
        assert backlog_col.has_more is True

        assert progress_col.count == 12
        assert len(progress_col.tickets) == 5
        assert progress_col.has_more is True

    def test_summary_view_unaffected_by_limit(self):
        """Summary view returns counts only, limit is ignored."""
        conn, cur = _mock_conn_and_cursor()
        cur.fetchall.return_value = [
            {"status": "backlog", "count": 50},
            {"status": "in_progress", "count": 20},
        ]

        result = self.service.board_view(conn, SCHEMA, view="summary", limit=5)

        assert result.limit_per_column is None
        backlog_col = next(c for c in result.columns if c.status == "backlog")
        assert backlog_col.count == 50
        assert len(backlog_col.tickets) == 0

    def test_total_reflects_all_tickets(self):
        """total should count all matching tickets, not just visible ones."""
        rows = [
            _make_ticket_row(id=i, status="backlog") for i in range(25)
        ]
        conn, cur = _mock_conn_and_cursor(rows)

        result = self.service.board_view(conn, SCHEMA, view="kanban", limit=5)

        assert result.total == 25


class TestBoardCompactView:
    """board_view compact mode returns minimal ticket data."""

    def setup_method(self):
        self.service = TicketService()
        self.service.archive_stale_tickets = MagicMock(return_value=0)

    def test_compact_returns_compact_tickets(self):
        """Compact view populates compact_tickets, not tickets."""
        rows = [
            _make_ticket_row(id=1, title="First", status="backlog", priority="high"),
            _make_ticket_row(id=2, title="Second", status="backlog", priority="low"),
        ]
        conn, cur = _mock_conn_and_cursor(rows)

        result = self.service.board_view(conn, SCHEMA, view="compact")

        backlog_col = next(c for c in result.columns if c.status == "backlog")
        assert len(backlog_col.compact_tickets) == 2
        assert len(backlog_col.tickets) == 0  # Regular tickets empty

        ct = backlog_col.compact_tickets[0]
        assert ct.id == 1
        assert ct.title == "First"
        assert ct.priority == "high"
        assert ct.type == "task"

    def test_compact_no_description(self):
        """Compact tickets should not include description."""
        rows = [
            _make_ticket_row(id=1, description="Very long description" * 100),
        ]
        conn, cur = _mock_conn_and_cursor(rows)

        result = self.service.board_view(conn, SCHEMA, view="compact")

        ct = result.columns[0].compact_tickets[0]
        assert not hasattr(ct, "description") or ct.model_fields.get("description") is None

    def test_compact_with_limit(self):
        """Compact view also respects limit."""
        rows = [
            _make_ticket_row(id=i, status="backlog") for i in range(20)
        ]
        conn, cur = _mock_conn_and_cursor(rows)

        result = self.service.board_view(conn, SCHEMA, view="compact", limit=3)

        backlog_col = next(c for c in result.columns if c.status == "backlog")
        assert backlog_col.count == 20
        assert len(backlog_col.compact_tickets) == 3
        assert backlog_col.has_more is True

    def test_compact_view_field_in_response(self):
        """Response should include view='compact'."""
        conn, cur = _mock_conn_and_cursor([])

        result = self.service.board_view(conn, SCHEMA, view="compact")

        assert result.view == "compact"


class TestBoardDescriptionTruncation:
    """Card excerpts are bounded at 100 chars — in their OWN field since
    STOMPY-1519; `description` itself is never cut."""

    def setup_method(self):
        self.service = TicketService()
        self.service.archive_stale_tickets = MagicMock(return_value=0)

    def test_preview_truncates_at_100_chars_leaving_description_whole(self):
        """Descriptions longer than 100 chars get a bounded preview."""
        long_desc = "x" * 150
        rows = [_make_ticket_row(id=1, description=long_desc)]
        conn, cur = _mock_conn_and_cursor(rows)

        result = self.service.board_view(conn, SCHEMA, view="kanban", limit=0)

        ticket = result.columns[0].tickets[0]
        assert len(ticket.description_preview) == 103  # 100 + "..."
        assert ticket.description_preview.endswith("...")
        assert ticket.description == long_desc

    def test_preserves_short_descriptions(self):
        """Descriptions under 100 chars are preserved."""
        short_desc = "x" * 80
        rows = [_make_ticket_row(id=1, description=short_desc)]
        conn, cur = _mock_conn_and_cursor(rows)

        result = self.service.board_view(conn, SCHEMA, view="kanban", limit=0)

        ticket = result.columns[0].tickets[0]
        assert ticket.description == short_desc
