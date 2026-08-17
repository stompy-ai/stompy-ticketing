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
    # Consistent with the rows we hand back — a fixture that lies about
    # totals can mask a metadata bug (Kimi #25).
    cur.fetchone.return_value = {"count": len(rows)}
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

        # Derived, never a literal 103 — the constant is load-bearing and a
        # hardcoded length would silently pin a stale bound (Kimi #25).
        assert ticket.description_preview == (
            LONG[: TicketService.BOARD_DESC_MAX_LENGTH] + TicketService.BOARD_DESC_ELLIPSIS
        )

    def test_exactly_at_the_limit_is_not_cut(self):
        """Boundary pin: an off-by-one (> vs >=) would otherwise pass the
        entire suite (Kimi #25)."""
        exact = "y" * TicketService.BOARD_DESC_MAX_LENGTH
        ticket = _first(self.service.board_view(_conn([_row(description=exact)]), SCHEMA, "kanban"))

        assert ticket.description == exact
        assert ticket.description_preview == exact
        assert not ticket.description_preview.endswith("...")

    def test_preview_is_never_longer_than_what_it_excerpts(self):
        """101-103 chars: cutting to 100 and adding an ellipsis produces a
        'preview' LONGER than the original. Send the original."""
        just_over = "z" * (TicketService.BOARD_DESC_MAX_LENGTH + 2)
        ticket = _first(
            self.service.board_view(_conn([_row(description=just_over)]), SCHEMA, "kanban")
        )

        assert ticket.description_preview == just_over
        assert len(ticket.description_preview) <= len(ticket.description)

    def test_empty_description_leaves_preview_none(self):
        ticket = _first(self.service.board_view(_conn([_row(description="")]), SCHEMA, "kanban"))

        assert ticket.description_preview is None

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
        column = next((c for c in board.columns if c.compact_tickets), None)
        assert column is not None, f"no compact tickets on the board: {board}"

        assert column.tickets == []
        card = column.compact_tickets[0]
        assert not hasattr(card, "description")
        assert not hasattr(card, "description_preview")


class TestPreviewIsBoardOnly:
    """"Set only on board responses" is a contract, so pin it — otherwise a
    future refactor could start populating it everywhere, or nowhere, with
    the suite still green (Kimi #25)."""

    def setup_method(self):
        self.service = TicketService()
        self.service.archive_stale_tickets = MagicMock(return_value=0)

    def test_single_ticket_read_leaves_preview_none(self):
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.return_value = _row()
        cur.fetchall.return_value = []  # history + links queries

        ticket = self.service.get_ticket(conn, SCHEMA, 1)

        assert ticket.description == LONG
        assert ticket.description_preview is None
