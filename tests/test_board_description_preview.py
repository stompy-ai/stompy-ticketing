"""A field named `description` must never be a card-shaped summary (STOMPY-1519).

Markus opened ticket #4 in the web UI on 2026-08-17 and read "...early
groundwork does not ..." — the text simply stopped. The web was innocent:
ticket-detail-dialog.tsx says "already carries the complete description, so
no extra fetch" and renders faithfully. board_view had already cut the
string to BOARD_DESC_MAX_LENGTH and appended "...".

Truncating in place makes every downstream consumer inherit a display value
under a data name. The card gets its own field instead.
"""

from unittest.mock import MagicMock

from stompy_ticketing.service import TicketService

FIXED_TIME = 1700000000.0
SCHEMA = "test_project"

LONG = "Build a PR and communications plan. Can start in parallel with ticket 2 — " + (
    "early groundwork does not depend on it. " * 6
)


def _row(id=1, description=LONG, status="backlog"):
    return {
        "id": id,
        "title": "Build the PR and communications plan",
        "description": description,
        "type": "feature",
        "status": status,
        "priority": "high",
        "assignee": None,
        "tags": None,
        "metadata": None,
        "session_id": "sess_123",
        "created_at": FIXED_TIME,
        "updated_at": FIXED_TIME,
        "closed_at": None,
        "content_hash": "abc123",
        "content_tsvector": None,
        "archived_at": None,
    }


def _conn(rows):
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = {"count": 0}
    return conn


def _first(board):
    for column in board.columns:
        if column.tickets:
            return column.tickets[0]
    raise AssertionError(f"no tickets on the board: {board}")


class TestBoardCarriesBothFullAndPreview:
    def setup_method(self):
        self.service = TicketService()
        self.service.archive_stale_tickets = MagicMock(return_value=0)

    def test_description_is_never_truncated_in_place(self):
        board = self.service.board_view(_conn([_row()]), SCHEMA, view="kanban")
        ticket = _first(board)

        assert ticket.description == LONG, (
            "board truncated `description` in place — the detail dialog reuses "
            "this row and would render a cut string (STOMPY-1519)"
        )
        assert not ticket.description.endswith("...")

    def test_card_preview_is_bounded_and_marked(self):
        board = self.service.board_view(_conn([_row()]), SCHEMA, view="kanban")
        ticket = _first(board)

        assert ticket.description_preview is not None
        assert len(ticket.description_preview) <= TicketService.BOARD_DESC_MAX_LENGTH + 3
        assert ticket.description_preview.endswith("...")
        # The preview is a prefix of the real thing, not a paraphrase.
        assert LONG.startswith(
            ticket.description_preview[: TicketService.BOARD_DESC_MAX_LENGTH]
        )

    def test_short_description_needs_no_ellipsis(self):
        board = self.service.board_view(_conn([_row(description="brief")]), SCHEMA, view="kanban")
        ticket = _first(board)

        assert ticket.description == "brief"
        assert ticket.description_preview == "brief"
        assert not ticket.description_preview.endswith("...")

    def test_missing_description_stays_none_on_both_fields(self):
        board = self.service.board_view(_conn([_row(description=None)]), SCHEMA, view="kanban")
        ticket = _first(board)

        assert ticket.description is None
        assert ticket.description_preview is None

    def test_compact_view_bounds_by_dropping_the_field_not_cutting_it(self):
        """Compact exists to bound MCP payloads, and it does so the right
        way already: CompactTicket carries no description at all. Pinned
        here so nobody 'helpfully' adds a truncated one."""
        board = self.service.board_view(_conn([_row()]), SCHEMA, view="compact")
        column = next(c for c in board.columns if c.compact_tickets)

        assert column.tickets == []
        card = column.compact_tickets[0]
        assert not hasattr(card, "description")
        assert not hasattr(card, "description_preview")
