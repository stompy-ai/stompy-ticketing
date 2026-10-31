"""The board is bounded in SQL, and agent doors get cards (STOMPY-3284).

Prod, 30 d to 2026-10-31: ticket_board payload p95 792 KB, max 6.8 MB, on a
schema of ~3,500 tickets. board_view fetched EVERY matching row with every
column (`SELECT *`), applied the per-column limit in Python, and shipped each
visible card with its whole description although the tool promises truncated
ones. It also ran the stale-archive sweep (a write) on every read.

The fake cursor (tests/board_fake.py) answers the board's queries the way Postgres would:
the per-column rank filter is honoured from the SQL's own parameter, so a
board that still fetches everything shows up as rows fetched, not as a
mock that happens to agree.
"""

import importlib
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from stompy_ticketing.service import TicketService
from tests.board_fake import board_conn as _conn
from tests.test_service import _make_ticket_row

SCHEMA = "test_project"
LONG = "Ship the bounded board. " * 400  # ~9.6 KB, like the descriptions on prod


def _service():
    svc = TicketService()
    svc.archive_stale_tickets = MagicMock(return_value=0)
    return svc


def _col(board, status):
    return next(c for c in board.columns if c.status == status)


class TestBoardIsBoundedInSql:
    def test_kanban_fetches_at_most_limit_rows_per_column(self):
        rows = [_make_ticket_row(id=i, status="backlog") for i in range(1, 26)] + [
            _make_ticket_row(id=100 + i, status="in_progress") for i in range(3)
        ]
        conn, cur = _conn(rows)

        board = _service().board_view(conn, SCHEMA, view="kanban")

        text, params = cur.row_query()
        assert "row_number() OVER (PARTITION BY status" in text
        assert params[-1] == TicketService.BOARD_DEFAULT_LIMIT
        assert cur.rows_fetched == 13  # 10 + 3, not 28
        assert (_col(board, "backlog").count, _col(board, "backlog").truncated_count) == (25, 15)
        assert _col(board, "backlog").has_more is True
        assert len(_col(board, "backlog").tickets) == 10
        assert (_col(board, "in_progress").count, _col(board, "in_progress").truncated_count) == (3, 0)
        assert board.total == 28

    def test_counts_come_from_one_group_by_not_from_the_fetched_rows(self):
        rows = [_make_ticket_row(id=i, status="backlog") for i in range(1, 11)]
        conn, cur = _conn(rows, counts={"backlog": 3500})

        board = _service().board_view(conn, SCHEMA, view="kanban")

        group_bys = [t for t, _ in cur.board_queries() if "GROUP BY status" in t]
        assert len(group_bys) == 1
        col = _col(board, "backlog")
        assert (col.count, len(col.tickets), col.truncated_count, col.has_more) == (
            3500, 10, 3490, True
        )
        assert board.total == 3500

    def test_compact_is_bounded_and_names_six_columns(self):
        rows = [_make_ticket_row(id=i, status="backlog") for i in range(1, 21)]
        conn, cur = _conn(rows)

        board = _service().board_view(conn, SCHEMA, view="compact", limit=3)

        text, params = cur.row_query()
        assert params[-1] == 3 and cur.rows_fetched == 3
        assert "description" not in text and "SELECT *" not in text
        col = _col(board, "backlog")
        assert (col.count, len(col.compact_tickets), col.truncated_count) == (20, 3, 17)

    def test_limit_zero_is_unbounded_in_sql(self):
        rows = [_make_ticket_row(id=i, status="backlog") for i in range(1, 31)]
        conn, cur = _conn(rows)

        board = _service().board_view(conn, SCHEMA, view="kanban", limit=0)

        text, _ = cur.row_query()
        assert "board_rank <=" not in text
        assert cur.rows_fetched == 30
        assert board.limit_per_column is None
        assert _col(board, "backlog").has_more is False

    def test_row_query_names_the_columns_a_card_reads(self):
        conn, cur = _conn([_make_ticket_row()])

        _service().board_view(conn, SCHEMA, view="kanban")

        text, _ = cur.row_query()
        assert "SELECT *" not in text
        for column in ("id", "title", "type", "status", "priority", "assignee", "tags",
                       "metadata", "created_by", "updated_at", "claimed_until"):
            assert column in text, column
        assert "content_tsvector" not in text and "content_hash" not in text


class TestCardsCarryThePreview:
    def test_card_fields_drop_the_body_and_keep_the_preview(self):
        conn, cur = _conn([_make_ticket_row(description=LONG)])

        board = _service().board_view(conn, SCHEMA, view="kanban", fields="card")

        text, params = cur.row_query()
        cutoff = TicketService.BOARD_DESC_MAX_LENGTH + len(TicketService.BOARD_DESC_ELLIPSIS)
        assert "LEFT(description, %s)" in text and params[0] == cutoff + 1
        card = _col(board, "backlog").tickets[0]
        assert card.description is None
        assert card.description_preview == (
            LONG[: TicketService.BOARD_DESC_MAX_LENGTH] + TicketService.BOARD_DESC_ELLIPSIS
        )

    def test_card_preview_of_a_short_body_is_the_body(self):
        conn, _ = _conn([_make_ticket_row(description="brief")])

        card = _col(_service().board_view(conn, SCHEMA, fields="card"), "backlog").tickets[0]

        assert card.description is None and card.description_preview == "brief"

    def test_full_fields_is_the_default_and_keeps_the_body_whole(self):
        """The web's detail dialog renders `description` from the board row
        (host GET /projects/{name}/tickets, STOMPY-1519): the default stays whole."""
        conn, cur = _conn([_make_ticket_row(description=LONG)])

        board = _service().board_view(conn, SCHEMA, view="kanban")

        assert "LEFT(" not in cur.row_query()[0]
        assert _col(board, "backlog").tickets[0].description == LONG

    def test_unknown_fields_is_refused(self):
        conn, _ = _conn([])
        try:
            _service().board_view(conn, SCHEMA, fields="everything")
        except ValueError:
            return
        raise AssertionError("fields='everything' was accepted")


class TestAgentDoorsAskForCards:
    def test_mcp_ticket_board_asks_for_cards(self):
        from stompy_ticketing.mcp_tools import register_ticketing_tools

        registered = {}
        mcp = MagicMock()
        mcp.tool = lambda: (lambda f: registered.setdefault(f.__name__, f))

        @contextmanager
        def db_ctx(project=None, require_write=True):
            yield MagicMock()

        with patch("stompy_ticketing.mcp_tools.TicketService") as MockService:
            svc = MockService.return_value
            svc.board_view.return_value = MagicMock(model_dump=MagicMock(return_value={}))
            register_ticketing_tools(
                mcp_instance=mcp, get_db_func=db_ctx,
                check_project_func=MagicMock(return_value=None),
                get_project_func=MagicMock(return_value="p"),
            )
            import asyncio

            asyncio.run(registered["ticket_board"](project="p"))

        assert svc.board_view.call_args.kwargs.get("fields") == "card"

    def test_plugin_rest_board_asks_for_cards(self):
        import asyncio

        from stompy_ticketing import api_routes

        with patch.object(api_routes, "_service") as svc, \
                patch.object(api_routes, "_get_db_for_project", MagicMock()), \
                patch.object(api_routes, "redact_payload", side_effect=lambda x: x):
            asyncio.run(api_routes.board_view(name="p", view="kanban", type=None,
                                              ticket_status=None, include_terminal=False,
                                              include_archived=False))

        assert svc.board_view.call_args.kwargs.get("fields") == "card"


DB = {"host": "h", "port": "5432", "dbname": "d"}


class TestStaleSweepIsThrottled:
    def _throttle_module(self):
        # The throttle the board shares with list and search (STOMPY-3288).
        return importlib.import_module("stompy_ticketing.stale_archive")

    def test_second_board_read_within_the_window_does_not_sweep(self):
        mod = self._throttle_module()
        svc = _service()
        with patch.object(mod, "_last_sweep", {}), patch.object(mod, "_clock", return_value=1000.0):
            svc.board_view(_conn([], dsn=DB)[0], SCHEMA, view="summary")
            svc.board_view(_conn([], dsn=DB)[0], SCHEMA, view="kanban")
        assert svc.archive_stale_tickets.call_count == 1

    def test_board_read_after_the_window_sweeps_again(self):
        mod = self._throttle_module()
        svc = _service()
        with patch.object(mod, "_last_sweep", {}), patch.object(mod, "_clock") as clock:
            clock.return_value = 1000.0
            svc.board_view(_conn([], dsn=DB)[0], SCHEMA, view="summary")
            clock.return_value = 1000.0 + mod.SWEEP_INTERVAL_SECONDS - 1
            svc.board_view(_conn([], dsn=DB)[0], SCHEMA, view="summary")
            clock.return_value = 1000.0 + mod.SWEEP_INTERVAL_SECONDS
            svc.board_view(_conn([], dsn=DB)[0], SCHEMA, view="summary")
        assert svc.archive_stale_tickets.call_count == 2

    def test_window_is_per_schema_and_per_database(self):
        """Tenants have their own Neon databases with same-named schemas: one
        tenant's sweep must not suppress another's."""
        mod = self._throttle_module()
        svc = _service()
        other_db = {"host": "tenant-neon", "port": "5432", "dbname": "d"}
        with patch.object(mod, "_last_sweep", {}), patch.object(mod, "_clock", return_value=1000.0):
            svc.board_view(_conn([], dsn=DB)[0], SCHEMA, view="summary")
            svc.board_view(_conn([], dsn=DB)[0], "other_schema", view="summary")
            svc.board_view(_conn([], dsn=other_db)[0], SCHEMA, view="summary")
        assert svc.archive_stale_tickets.call_count == 3

    def test_a_failing_sweep_never_fails_the_board(self):
        mod = self._throttle_module()
        svc = _service()
        svc.archive_stale_tickets.side_effect = RuntimeError("read-only replica")
        with patch.object(mod, "_last_sweep", {}), patch.object(mod, "_clock", return_value=1000.0):
            board = svc.board_view(_conn([_make_ticket_row()])[0], SCHEMA, view="kanban")
        assert board.total == 1
