"""STOMPY-1923 / 1895 / 1908: ticket payloads return what the caller asked
for, not everything the row holds.

Measured on prod (1923): ticket(list, limit=60) = 389 KB, 90% description
bodies, description_preview null on every row. Closing one bug (1895) took
five move() calls (the refusal names only the next hop) and ~20 KB (every
transition returns the whole ticket + history). Every row also emits eight
null/empty keys (1908).

RED first: written before list/search grew `fields`, before `to_card`,
before `find_transition_path`, before move() slimmed its response, and
before `_safe_json` dropped null/empty values.

Contract pinned here:
  * list/search return CARD rows by default: no description, no history,
    a bounded description_preview; `fields="full"` restores the body;
    `get` is unchanged and returns the full record.
  * a refused transition to a REACHABLE target names the full path and,
    for a terminal target, points at action='close' which walks it.
  * MCP move/close answer with the status change (id, status,
    previous_status, updated_at), not the record.
  * the MCP encoder omits None values and empty collections, so a board
    summary column has no `tickets`/`compact_tickets` keys.
  * REST /search honours ?fields=full and defaults to cards.
"""

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from stompy_ticketing.api_routes import configure_routes, router
from stompy_ticketing.mcp_tools import _safe_json, register_ticketing_tools
from stompy_ticketing.models import TicketListFilters, TicketResponse
from stompy_ticketing.service import (
    InvalidTransitionError,
    TicketService,
    find_transition_path,
    validate_transition,
)

FIXED_TIME = 1700000000.0
SCHEMA = "test_project"
LONG = "x" * 400


def _row(**overrides):
    row = {
        "id": 1,
        "title": "Test ticket",
        "description": LONG,
        "type": "bug",
        "status": "triage",
        "priority": "medium",
        "assignee": None,
        "tags": None,
        "metadata": None,
        "session_id": None,
        "created_by": "51",
        "created_at": FIXED_TIME,
        "updated_at": FIXED_TIME,
        "closed_at": None,
        "content_hash": "abc",
        "content_tsvector": None,
        "archived_at": None,
    }
    row.update(overrides)
    return row


def _conn(rows):
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    cur.fetchall.return_value = rows
    cur.fetchone.return_value = {"count": len(rows)}
    return conn, cur


# --------------------------------------------------------------------------- #
# Core: cards vs full                                                         #
# --------------------------------------------------------------------------- #


class TestListAndSearchReturnCards:
    def setup_method(self):
        self.service = TicketService()

    def test_list_default_rows_are_cards(self):
        conn, cur = _conn([_row(id=1), _row(id=2)])
        # list_tickets runs: archive sweep, main select, count, by_status, by_type
        cur.fetchall.side_effect = [[], [_row(id=1), _row(id=2)], [], []]

        result = self.service.list_tickets(conn, SCHEMA, TicketListFilters())

        for t in result.tickets:
            assert t.description is None, "cards must not carry the body"
            assert t.description_preview == "x" * 100 + "...", "cards carry the bounded excerpt"
            assert t.history == [] and t.links == []

    def test_list_fields_full_keeps_the_body(self):
        conn, cur = _conn([_row()])
        cur.fetchall.side_effect = [[], [_row()], [], []]

        result = self.service.list_tickets(conn, SCHEMA, TicketListFilters(), fields="full")

        assert result.tickets[0].description == LONG

    def test_search_default_rows_are_cards(self):
        conn, cur = _conn([_row()])
        cur.fetchall.side_effect = [[], [_row()]]  # archive sweep, results

        result = self.service.search_tickets(conn, SCHEMA, "test")

        assert result.tickets[0].description is None
        assert result.tickets[0].description_preview.startswith("x" * 100)

    def test_search_fields_full_keeps_the_body(self):
        conn, cur = _conn([_row()])
        cur.fetchall.side_effect = [[], [_row()]]

        result = self.service.search_tickets(conn, SCHEMA, "test", fields="full")

        assert result.tickets[0].description == LONG

    def test_to_card_is_a_pure_projection(self):
        full = TicketResponse(id=1, title="t", type="task", status="backlog", priority="low", description="short")
        card = TicketService.to_card(full)
        assert card.description is None
        assert card.description_preview == "short", "a short body is its own preview, uncut"
        assert full.description == "short", "the source record is not mutated"

    def test_get_still_returns_the_full_record(self):
        conn, cur = _conn([])
        cur.fetchone.return_value = _row()
        cur.fetchall.return_value = []

        result = self.service.get_ticket(conn, SCHEMA, 1)

        assert result.description == LONG


# --------------------------------------------------------------------------- #
# Core: errors that teach the path                                            #
# --------------------------------------------------------------------------- #


class TestRefusalNamesThePath:
    def test_find_transition_path_walks_the_graph(self):
        assert find_transition_path("bug", "triage", "resolved") == ["confirmed", "in_progress", "resolved"]
        assert find_transition_path("task", "backlog", "done") == ["done"]
        # Terminals reopen, so even a closed bug has a path back into work.
        assert find_transition_path("bug", "resolved", "in_progress") == ["triage", "confirmed", "in_progress"]
        assert find_transition_path("task", "done", "nowhere") is None

    def test_refusal_to_a_reachable_terminal_names_the_path_and_close(self):
        with pytest.raises(InvalidTransitionError) as exc:
            validate_transition("bug", "triage", "resolved")
        msg = str(exc.value)
        assert "confirmed → in_progress → resolved" in msg
        assert "action='close'" in msg and "resolution='resolved'" in msg

    def test_refusal_to_a_reachable_non_terminal_names_the_path_only(self):
        with pytest.raises(InvalidTransitionError) as exc:
            validate_transition("bug", "triage", "in_progress")
        msg = str(exc.value)
        assert "confirmed → in_progress" in msg
        assert "action='close'" not in msg

    def test_refusal_from_a_terminal_names_the_reopen_path(self):
        with pytest.raises(InvalidTransitionError) as exc:
            validate_transition("task", "done", "in_progress")
        assert "backlog → in_progress" in str(exc.value)

    def test_refusal_to_an_unreachable_target_says_so(self):
        with pytest.raises(InvalidTransitionError) as exc:
            validate_transition("task", "done", "nowhere")
        assert "not reachable" in str(exc.value)


# --------------------------------------------------------------------------- #
# MCP door                                                                    #
# --------------------------------------------------------------------------- #


def _mock_mcp():
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


def _register():
    mcp = _mock_mcp()

    @contextmanager
    def db_ctx(project=None, require_write=True):
        yield MagicMock()

    with patch("stompy_ticketing.mcp_tools.TicketService") as MockService:
        svc = MagicMock()
        svc.to_card = TicketService.to_card
        MockService.return_value = svc
        MockService.to_card = TicketService.to_card
        register_ticketing_tools(
            mcp_instance=mcp,
            get_db_func=db_ctx,
            check_project_func=MagicMock(return_value=None),
            get_project_func=MagicMock(return_value=SCHEMA),
        )
        return mcp._registered_tools, svc


def _run(coro):
    return asyncio.run(coro)


def _parse(text):
    try:
        from toon import decode

        return decode(text)
    except Exception:
        return json.loads(text)


class TestEncoderOmitsNoise:
    def test_none_and_empty_collections_are_dropped_recursively(self):
        payload = {
            "id": 1,
            "assignee": None,
            "history": [],
            "metadata": {},
            "total": 0,
            "nested": {"tickets": [], "count": 3, "x": None},
        }
        out = _parse(_safe_json(payload))
        assert out == {"id": 1, "total": 0, "nested": {"count": 3}}

    def test_a_ticket_response_with_no_extras_carries_none_of_the_noise_keys(self):
        t = TicketResponse(id=1, title="t", type="task", status="backlog", priority="medium")
        out = _parse(_safe_json(t))
        for noise in ("assignee", "metadata", "history", "links", "context_links", "closed_at", "archived_at", "description_preview"):
            assert noise not in out, noise


class TestMoveAnswersWithTheChange:
    def test_move_returns_status_change_not_the_record(self):
        tools, svc = _register()
        from stompy_ticketing.models import TicketHistoryEntry

        svc.transition_ticket.return_value = TicketResponse(
            id=7, title="t", type="bug", status="confirmed", priority="high",
            description=LONG, updated_at=FIXED_TIME,
            history=[TicketHistoryEntry(id=1, field_name="status", old_value="triage", new_value="confirmed")],
        )
        out = _parse(_run(tools["ticket"](action="move", ticket_id=7, status="confirmed", project=SCHEMA)))

        assert out["status"] == "transitioned"
        assert out["ticket"]["id"] == 7
        assert out["ticket"]["status"] == "confirmed"
        assert out["ticket"]["previous_status"] == "triage"
        assert "description" not in out["ticket"]
        assert "history" not in out["ticket"]

    def test_previous_status_is_the_latest_transition_whatever_the_history_order(self):
        tools, svc = _register()
        from stompy_ticketing.models import TicketHistoryEntry

        # Oldest-first here; _fetch_history is newest-first — both must work.
        svc.transition_ticket.return_value = TicketResponse(
            id=7, title="t", type="bug", status="in_progress", priority="high", updated_at=FIXED_TIME,
            history=[
                TicketHistoryEntry(id=1, field_name="status", old_value="triage", new_value="confirmed", changed_at=1.0),
                TicketHistoryEntry(id=2, field_name="status", old_value="confirmed", new_value="in_progress", changed_at=2.0),
            ],
        )
        out = _parse(_run(tools["ticket"](action="move", ticket_id=7, status="in_progress", project=SCHEMA)))
        assert out["ticket"]["previous_status"] == "confirmed"

    def test_close_returns_status_change_not_the_record(self):
        tools, svc = _register()
        svc.close_ticket.return_value = TicketResponse(
            id=7, title="t", type="bug", status="resolved", priority="high",
            description=LONG, closed_at=FIXED_TIME, updated_at=FIXED_TIME,
        )
        out = _parse(_run(tools["ticket"](action="close", ticket_id=7, project=SCHEMA)))

        assert out["status"] == "closed"
        assert out["ticket"]["status"] == "resolved"
        assert "description" not in out["ticket"]


class TestListAndSearchDoors:
    def _card_result(self):
        from stompy_ticketing.models import SearchResult, TicketListResponse

        full = TicketResponse(id=1, title="t", type="bug", status="triage", priority="medium", description=LONG)
        return full, TicketListResponse, SearchResult

    def test_list_passes_fields_through_and_defaults_to_card(self):
        tools, svc = _register()
        full, TicketListResponse, _ = self._card_result()
        svc.list_tickets.return_value = TicketListResponse(tickets=[TicketService.to_card(full)], total=1)

        out = _parse(_run(tools["ticket"](action="list", project=SCHEMA)))

        assert svc.list_tickets.call_args.kwargs.get("fields", "card") == "card"
        assert "description" not in out["tickets"][0]
        assert out["tickets"][0]["description_preview"].endswith("...")

    def test_list_fields_full_is_forwarded(self):
        tools, svc = _register()
        full, TicketListResponse, _ = self._card_result()
        svc.list_tickets.return_value = TicketListResponse(tickets=[full], total=1)

        _run(tools["ticket"](action="list", project=SCHEMA, fields="full"))

        assert svc.list_tickets.call_args.kwargs["fields"] == "full"

    def test_search_regex_matches_the_body_but_returns_cards(self):
        tools, svc = _register()
        full, _, SearchResult = self._card_result()
        svc.search_tickets.return_value = SearchResult(tickets=[full], total=1, query="t")

        out = _parse(_run(tools["ticket_search"](query="t", regex="x{50}", project=SCHEMA)))

        assert svc.search_tickets.call_args.kwargs["fields"] == "full", "regex needs the body to match against"
        assert out["total"] == 1
        assert "description" not in out["tickets"][0], "but the caller gets cards"


# --------------------------------------------------------------------------- #
# REST door                                                                   #
# --------------------------------------------------------------------------- #


def _app(conn):
    @contextmanager
    def ctx(project=None, require_write=True):
        yield conn

    configure_routes(get_db_func=ctx)
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.mark.asyncio
class TestRestSearchFields:
    async def test_search_defaults_to_cards(self):
        conn, cur = _conn([_row()])
        cur.fetchall.side_effect = [[], [_row()]]
        async with AsyncClient(transport=ASGITransport(app=_app(conn)), base_url="http://t") as client:
            r = await client.get(f"/projects/{SCHEMA}/tickets/search", params={"query": "test"})
        assert r.status_code == 200
        row = r.json()["tickets"][0]
        assert row["description"] is None
        assert row["description_preview"].endswith("...")

    async def test_search_fields_full_returns_the_body(self):
        conn, cur = _conn([_row()])
        cur.fetchall.side_effect = [[], [_row()]]
        async with AsyncClient(transport=ASGITransport(app=_app(conn)), base_url="http://t") as client:
            r = await client.get(f"/projects/{SCHEMA}/tickets/search", params={"query": "test", "fields": "full"})
        assert r.json()["tickets"][0]["description"] == LONG
