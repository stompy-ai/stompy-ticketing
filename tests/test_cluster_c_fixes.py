"""Regression tests for Cluster C fixes (Stompy ticket #462).

Bugs covered:
- #168 — ticket_link IntegrityError surfaces ALREADY_LINKED error_type
- #151 — BoardColumn includes truncated_count field
- #133 — ticket_board retries once on OperationalError
- #173 — ticket_board kanban cards include links + context_links
- #180 — list aggregates (by_status/by_type) respect grep filter
- #186 — close action's resolution param description names valid terminals
"""

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from psycopg2 import IntegrityError, OperationalError

from stompy_ticketing.mcp_tools import register_ticketing_tools
from stompy_ticketing.models import (
    BoardColumn,
    BoardView,
    ContextLinkResponse,
    TicketLinkResponse,
    TicketListResponse,
    TicketResponse,
)
from stompy_ticketing.service import LinkAlreadyExistsError, TicketService

FIXED_TIME = 1700000000.0
SCHEMA = "test_project"


def _make_mock_mcp():
    mock = MagicMock()
    registered = {}

    def tool_decorator():
        def decorator(func):
            registered[func.__name__] = func
            return func
        return decorator

    mock.tool = tool_decorator
    mock._registered_tools = registered
    return mock


def _register(mock_svc=None, db_factory=None):
    """Register tools and return them.

    db_factory: optional callable(call_index) -> conn yielded by ctx mgr.
    Used to give different connections on retry attempts.
    """
    mcp = _make_mock_mcp()
    check_project = MagicMock(return_value=None)
    get_project = MagicMock(return_value="test-project")

    call_count = {"n": 0}

    @contextmanager
    def db_ctx(project=None, require_write=True):
        n = call_count["n"]
        call_count["n"] += 1
        if db_factory is not None:
            conn = db_factory(n)
        else:
            conn = MagicMock()
        yield conn

    if mock_svc is not None:
        with patch("stompy_ticketing.mcp_tools.TicketService", return_value=mock_svc):
            register_ticketing_tools(
                mcp_instance=mcp,
                get_db_func=db_ctx,
                check_project_func=check_project,
                get_project_func=get_project,
            )
    else:
        register_ticketing_tools(
            mcp_instance=mcp,
            get_db_func=db_ctx,
            check_project_func=check_project,
            get_project_func=get_project,
        )

    return mcp._registered_tools, call_count


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# --------------------------------------------------------------------------- #
# #168 — Structured ALREADY_LINKED error                                      #
# --------------------------------------------------------------------------- #


class TestBug168AlreadyLinkedErrorType:
    def test_link_already_exists_returns_already_linked_error_type(self):
        mock_svc = MagicMock()
        mock_svc.add_link.side_effect = LinkAlreadyExistsError(
            "Link already exists between ticket #1 and #2"
        )

        tools, _ = _register(mock_svc=mock_svc)
        result_text = _run(
            tools["ticket_link"](action="add", ticket_id=1, target_id=2)
        )
        result = json.loads(result_text)

        assert result["error_type"] == "ALREADY_LINKED"
        assert "already exists" in result["error"].lower()

    def test_context_link_already_exists_returns_already_linked_error_type(self):
        mock_svc = MagicMock()
        mock_svc.add_context_link.side_effect = LinkAlreadyExistsError(
            "Context link already exists: ticket #1 ↔ topic_x"
        )

        tools, _ = _register(mock_svc=mock_svc)
        result_text = _run(
            tools["ticket_link"](action="add", ticket_id=1, context_label="topic_x")
        )
        result = json.loads(result_text)

        assert result["error_type"] == "ALREADY_LINKED"

    def test_link_already_exists_subclasses_value_error(self):
        # Backward-compat: any code catching ValueError still works.
        try:
            raise LinkAlreadyExistsError("test")
        except ValueError as e:
            assert str(e) == "test"

    def test_service_add_link_raises_link_already_exists_on_integrity_error(self):
        # Patch psycopg2 IntegrityError into add_link, verify it's converted.
        from stompy_ticketing.models import LinkType, TicketLinkCreate

        svc = TicketService()
        conn = MagicMock()
        cur = MagicMock()
        conn.cursor.return_value = cur
        cur.execute.side_effect = IntegrityError("duplicate key")

        try:
            svc.add_link(
                conn, SCHEMA, source_id=1,
                data=TicketLinkCreate(target_id=2, link_type=LinkType.related),
            )
        except LinkAlreadyExistsError as e:
            assert "already exists" in str(e).lower()
        else:
            raise AssertionError("Expected LinkAlreadyExistsError")


# --------------------------------------------------------------------------- #
# #151 — BoardColumn truncated_count                                          #
# --------------------------------------------------------------------------- #


class TestBug151TruncatedCount:
    def test_board_column_default_truncated_count_is_zero(self):
        col = BoardColumn(status="backlog", count=0)
        assert col.truncated_count == 0

    def test_board_column_accepts_truncated_count(self):
        col = BoardColumn(status="backlog", count=15, truncated_count=5)
        assert col.truncated_count == 5
        assert col.count == 15

    def test_board_view_kanban_column_reports_truncated_count(self):
        # 12 backlog rows, default kanban limit is 10 → 2 truncated.
        from tests.test_service import _make_ticket_row, _mock_conn_and_cursor

        rows = [_make_ticket_row(id=i, status="backlog") for i in range(1, 13)]
        conn, cur = _mock_conn_and_cursor(rows=rows)
        cur.fetchall.return_value = rows

        svc = TicketService()
        result = svc.board_view(conn, SCHEMA, view="kanban")

        backlog_col = next(c for c in result.columns if c.status == "backlog")
        assert backlog_col.count == 12
        assert len(backlog_col.tickets) == 10  # BOARD_DEFAULT_LIMIT
        assert backlog_col.truncated_count == 2
        assert backlog_col.has_more is True

    def test_board_view_no_truncation_when_within_limit(self):
        from tests.test_service import _make_ticket_row, _mock_conn_and_cursor

        rows = [_make_ticket_row(id=i, status="backlog") for i in range(1, 4)]
        conn, cur = _mock_conn_and_cursor(rows=rows)
        cur.fetchall.return_value = rows

        svc = TicketService()
        result = svc.board_view(conn, SCHEMA, view="kanban")

        backlog_col = next(c for c in result.columns if c.status == "backlog")
        assert backlog_col.truncated_count == 0
        assert backlog_col.has_more is False


# --------------------------------------------------------------------------- #
# #133 — ticket_board retries once on OperationalError                        #
# --------------------------------------------------------------------------- #


class TestBug133BoardRetry:
    def _board_view_response(self):
        # Build a minimal valid BoardView for the second attempt to return.
        return BoardView(columns=[], total=0, view="kanban")

    def test_board_retries_once_on_operational_error(self):
        mock_svc = MagicMock()
        # First call: OperationalError; second call: success.
        success = self._board_view_response()
        mock_svc.board_view.side_effect = [
            OperationalError("SSL connection has been closed unexpectedly"),
            success,
        ]

        tools, calls = _register(mock_svc=mock_svc)
        result_text = _run(tools["ticket_board"]())
        # Should NOT contain an error key — retry succeeded.
        assert '"error"' not in result_text
        # Confirm board_view was called twice and a fresh ctx mgr was opened
        # for each attempt (calls[n] increments per get_db enter).
        assert mock_svc.board_view.call_count == 2
        assert calls["n"] == 2

    def test_board_does_not_retry_more_than_once(self):
        mock_svc = MagicMock()
        mock_svc.board_view.side_effect = OperationalError("conn closed")

        tools, _ = _register(mock_svc=mock_svc)
        result_text = _run(tools["ticket_board"]())
        result = json.loads(result_text)

        # After one failed retry, the error surfaces.
        assert "error" in result
        assert result["error_type"] == "OperationalError"
        assert mock_svc.board_view.call_count == 2


# --------------------------------------------------------------------------- #
# #173 — Board cards include links + context_links                            #
# --------------------------------------------------------------------------- #


class TestBug173BoardLinks:
    def test_board_view_populates_links_on_visible_cards(self):
        from tests.test_service import _make_ticket_row, _mock_conn_and_cursor

        rows = [
            _make_ticket_row(id=10, status="backlog"),
            _make_ticket_row(id=11, status="backlog"),
        ]
        conn, cur = _mock_conn_and_cursor(rows=rows)

        # Provide rows for: archive trigger SELECT, archived count SELECT,
        # main board SELECT, ticket_links bulk SELECT, ticket_context_links bulk SELECT
        # The archive trigger fetches rows; we fall through with empty fetchall by default.
        # We need fetchall to return:
        #   - main board rows (for the main SELECT *)
        #   - bulk links rows
        #   - bulk context_links rows
        link_row = {
            "id": 1, "source_id": 10, "target_id": 99,
            "link_type": "blocks", "created_at": FIXED_TIME,
            "target_title": "Linked", "target_status": "in_progress",
        }
        ctx_link_row = {
            "id": 1, "ticket_id": 11, "context_label": "topic_x",
            "context_version": "latest", "link_type": "implements",
            "created_at": FIXED_TIME,
            "ticket_title": "Foo", "ticket_status": "backlog",
        }
        # Sequence of fetchall returns:
        # 1. archive_stale_tickets stale rows -> []
        # 2. main board rows -> rows
        # 3. bulk link fetch -> [link_row]
        # 4. bulk context_link fetch -> [ctx_link_row]
        cur.fetchall.side_effect = [[], rows, [link_row], [ctx_link_row]]
        # archived_count fetchone returns 0
        cur.fetchone.return_value = {"count": 0}

        svc = TicketService()
        result = svc.board_view(conn, SCHEMA, view="kanban")

        backlog_col = next(c for c in result.columns if c.status == "backlog")
        ticket_10 = next(t for t in backlog_col.tickets if t.id == 10)
        ticket_11 = next(t for t in backlog_col.tickets if t.id == 11)

        assert len(ticket_10.links) == 1
        assert ticket_10.links[0].target_id == 99
        assert ticket_10.links[0].link_type == "blocks"
        assert ticket_10.context_links == []

        assert ticket_11.links == []
        assert len(ticket_11.context_links) == 1
        assert ticket_11.context_links[0].context_label == "topic_x"

    def test_compact_view_skips_link_fetch(self):
        # Compact tickets are intentionally minimal — no link fetch.
        from tests.test_service import _make_ticket_row, _mock_conn_and_cursor

        rows = [_make_ticket_row(id=1, status="backlog")]
        conn, cur = _mock_conn_and_cursor(rows=rows)
        cur.fetchall.side_effect = [[], rows]
        cur.fetchone.return_value = {"count": 0}

        svc = TicketService()
        result = svc.board_view(conn, SCHEMA, view="compact")

        # Should succeed without consuming extra fetchall results
        assert result.total == 1
        # Only 2 fetchall calls: archive stale + main board (no link fetch)
        assert cur.fetchall.call_count == 2


# --------------------------------------------------------------------------- #
# #180 — list aggregates respect grep filter                                  #
# --------------------------------------------------------------------------- #


class TestBug180GrepAggregates:
    def test_grep_filter_recomputes_by_status(self):
        # Pre-grep: 3 tickets (2 backlog, 1 in_progress, all type=task).
        # Grep "auth*" matches only "auth login" and "auth logout".
        mock_svc = MagicMock()
        mock_result = TicketListResponse(
            tickets=[
                TicketResponse(id=1, title="auth login", type="task", status="backlog", priority="medium"),
                TicketResponse(id=2, title="auth logout", type="task", status="in_progress", priority="medium"),
                TicketResponse(id=3, title="search", type="task", status="backlog", priority="medium"),
            ],
            total=3,
            limit=20,
            offset=0,
            has_more=False,
            by_status={"backlog": 2, "in_progress": 1},
            by_type={"task": 3},
        )
        mock_svc.list_tickets.return_value = mock_result

        tools, _ = _register(mock_svc=mock_svc)
        # Use json fallback path by checking presence of expected keys.
        from toon import decode as toon_decode
        raw = _run(tools["ticket"](action="list", grep="auth*"))
        try:
            data = toon_decode(raw)
        except Exception:
            data = json.loads(raw)

        # After grep, only 2 tickets remain (backlog + in_progress).
        # Aggregates should reflect the filtered set.
        assert data["total"] == 2
        assert data["by_status"] == {"backlog": 1, "in_progress": 1}
        assert data["by_type"] == {"task": 2}

    def test_no_grep_keeps_original_aggregates(self):
        mock_svc = MagicMock()
        mock_result = TicketListResponse(
            tickets=[
                TicketResponse(id=1, title="a", type="task", status="backlog", priority="medium"),
            ],
            total=1,
            limit=20,
            offset=0,
            has_more=False,
            by_status={"backlog": 1},
            by_type={"task": 1},
        )
        mock_svc.list_tickets.return_value = mock_result

        tools, _ = _register(mock_svc=mock_svc)
        from toon import decode as toon_decode
        raw = _run(tools["ticket"](action="list"))
        try:
            data = toon_decode(raw)
        except Exception:
            data = json.loads(raw)

        assert data["by_status"] == {"backlog": 1}
        assert data["by_type"] == {"task": 1}


# --------------------------------------------------------------------------- #
# #186 — close resolution param description clarity                           #
# --------------------------------------------------------------------------- #


class TestBug186ResolutionParamDescription:
    def test_resolution_description_lists_terminals_per_type(self):
        # Inspect the registered ticket tool's resolution param Annotated metadata.
        import inspect
        from typing import get_type_hints
        from stompy_ticketing.mcp_tools import register_ticketing_tools

        tools, _ = _register()
        ticket_fn = tools["ticket"]
        sig = inspect.signature(ticket_fn)
        resolution_param = sig.parameters["resolution"]
        # Annotated metadata is in the second arg of __metadata__
        ann = resolution_param.annotation
        # The description should mention each ticket type's terminals
        meta = getattr(ann, "__metadata__", ())
        desc = " ".join(str(m) for m in meta)
        # Required: be unambiguous about valid values per type.
        assert "resolved" in desc and "wont_fix" in desc
        assert "done" in desc and "cancelled" in desc
        assert "shipped" in desc and "rejected" in desc
        assert "decided" in desc and "deferred" in desc
