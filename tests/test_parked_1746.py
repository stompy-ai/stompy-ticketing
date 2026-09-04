"""STOMPY-1746: the PARKED state — a backlog can say "deliberately not now".

Before this, a ticket could only be worked or killed: every non-decision
type's only "not now" was semantically wont_fix, so 148 tickets nobody
would kill sat in backlog indistinguishable from real queue. And
ticket(action="archive", ticket_id=N) silently ignored ticket_id and
reported "Archived 0" — a silent no-op on an accepted parameter.

RED first: written before STATE_MACHINES had a `parked` status, before
transition_ticket took reason/revisit_by, and before the archive branch
refused ticket_id.

Contract pinned here:
  * parked is reachable from every PRE-WORK status of every type and
    reopens to those same statuses in one step; never from in_progress or
    a terminal. It is NOT terminal: closed_at stays NULL, auto-archive
    never touches it.
  * parking REQUIRES a reason; revisit_by is optional and must be an ISO
    date. Both live in metadata.parked while parked (cleared on reopen)
    and the reason is written to ticket_history permanently.
  * board views hide parked with the terminals unless include_terminal.
  * both doors (MCP move/batch_move, REST /move and /batch/move) carry
    reason + revisit_by; archive on the MCP door refuses ticket_id(s).
"""

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg2 import sql as psql

from stompy_ticketing.api_routes import configure_routes, router
from stompy_ticketing.mcp_tools import register_ticketing_tools
from stompy_ticketing.models import BatchMoveRequest, TicketResponse, TicketTransition
from stompy_ticketing.service import (
    PARKED,
    STATE_MACHINES,
    ParkArgumentError,
    TicketService,
    get_hidden_statuses,
    get_terminal_statuses,
    validate_transition,
)

FIXED_TIME = 1700000000.0
SCHEMA = "test_project"

PRE_WORK = [
    ("task", "backlog"),
    ("bug", "triage"),
    ("bug", "confirmed"),
    ("feature", "proposed"),
    ("feature", "approved"),
    ("decision", "open"),
]


def _row(**overrides):
    row = {
        "id": 1,
        "title": "Test ticket",
        "description": "d",
        "type": "task",
        "status": "backlog",
        "priority": "medium",
        "assignee": None,
        "tags": None,
        "metadata": None,
        "session_id": None,
        "created_at": FIXED_TIME,
        "updated_at": FIXED_TIME,
        "closed_at": None,
        "content_hash": "abc",
        "content_tsvector": None,
        "archived_at": None,
    }
    row.update(overrides)
    return row


def _conn():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur
    cur.fetchall.return_value = []
    return conn, cur


def _sql_text(composed):
    if isinstance(composed, psql.Composable):
        # Cheap and dependency-free: join the string parts of the Composed.
        parts = []
        for part in composed._wrapped if hasattr(composed, "_wrapped") else [composed]:
            if isinstance(part, psql.SQL):
                parts.append(part._wrapped)
            elif isinstance(part, psql.Identifier):
                parts.append(".".join(f'"{s}"' for s in part._wrapped))
            elif isinstance(part, psql.Composed):
                parts.append(_sql_text(part))
            else:
                parts.append(str(part))
        return "".join(parts)
    return str(composed)


def _executed(cur, keyword):
    """(sql_text, params) of the first cur.execute whose SQL contains keyword."""
    for call in cur.execute.call_args_list:
        text = _sql_text(call.args[0])
        if keyword in text:
            return text, (call.args[1] if len(call.args) > 1 else None)
    raise AssertionError(f"no execute() containing {keyword!r}")


# --------------------------------------------------------------------------- #
# State machine                                                               #
# --------------------------------------------------------------------------- #


class TestParkedInStateMachine:
    @pytest.mark.parametrize("ticket_type,from_status", PRE_WORK)
    def test_pre_work_statuses_can_park(self, ticket_type, from_status):
        assert validate_transition(ticket_type, from_status, PARKED) is True

    @pytest.mark.parametrize("ticket_type,to_status", PRE_WORK)
    def test_parked_reopens_in_one_step(self, ticket_type, to_status):
        assert validate_transition(ticket_type, PARKED, to_status) is True

    def test_parked_is_not_terminal(self):
        for ticket_type in STATE_MACHINES:
            assert PARKED not in get_terminal_statuses(ticket_type)

    def test_in_progress_cannot_park(self):
        for ticket_type in ("task", "bug", "feature"):
            assert (
                validate_transition(ticket_type, "in_progress", PARKED, raise_on_invalid=False)
                is False
            )

    def test_terminals_cannot_park(self):
        for ticket_type, sm in STATE_MACHINES.items():
            for terminal in sm["terminal"]:
                assert (
                    validate_transition(ticket_type, terminal, PARKED, raise_on_invalid=False)
                    is False
                ), f"{ticket_type}.{terminal} must not park"

    def test_hidden_statuses_are_terminals_plus_parked(self):
        assert get_hidden_statuses("bug") == ["parked", "resolved", "wont_fix"]
        everything = get_hidden_statuses()
        assert PARKED in everything
        for sm in STATE_MACHINES.values():
            for terminal in sm["terminal"]:
                assert terminal in everything


# --------------------------------------------------------------------------- #
# transition_ticket                                                           #
# --------------------------------------------------------------------------- #


class TestTransitionToParked:
    def setup_method(self):
        self.service = TicketService()

    def test_park_requires_reason(self):
        conn, cur = _conn()
        cur.fetchone.return_value = _row(status="backlog")

        with pytest.raises(ParkArgumentError, match="reason"):
            self.service.transition_ticket(conn, SCHEMA, 1, PARKED)

        # Nothing was written: the refusal happens before the UPDATE and the history INSERT.
        assert not any(
            "UPDATE" in _sql_text(c.args[0]) or "INSERT" in _sql_text(c.args[0])
            for c in cur.execute.call_args_list
        )
        conn.commit.assert_not_called()

    def test_park_rejects_non_iso_revisit_by(self):
        conn, cur = _conn()
        cur.fetchone.return_value = _row(status="backlog")

        with pytest.raises(ParkArgumentError, match="revisit_by"):
            self.service.transition_ticket(
                conn, SCHEMA, 1, PARKED, reason="later", revisit_by="next spring"
            )

    @patch("stompy_ticketing.service.time")
    def test_park_writes_metadata_and_history_and_no_closed_at(self, mock_time):
        mock_time.time.return_value = FIXED_TIME
        current = _row(status="backlog", metadata=json.dumps({"kept": 1}))
        parked_meta = {
            "kept": 1,
            "parked": {
                "reason": "pre-beta expansion", "revisit_by": "2026-12-01",
                "parked_at": FIXED_TIME, "from_status": "backlog",
            },
        }
        updated = _row(status=PARKED, metadata=json.dumps(parked_meta))
        conn, cur = _conn()
        cur.fetchone.side_effect = [current, updated]

        result = self.service.transition_ticket(
            conn, SCHEMA, 1, PARKED,
            changed_by="51", reason="pre-beta expansion", revisit_by="2026-12-01",
        )

        assert result.status == PARKED
        assert result.closed_at is None
        assert result.metadata["parked"]["reason"] == "pre-beta expansion"

        _, params = _executed(cur, "UPDATE")
        assert params[0] == PARKED
        assert params[2] is None, "parked is not terminal: closed_at stays NULL"
        written_meta = json.loads(params[3])
        assert written_meta["kept"] == 1, "existing metadata keys survive parking"
        assert written_meta["parked"] == parked_meta["parked"]

        history_rows = [
            c.args[1] for c in cur.execute.call_args_list
            if "INSERT INTO" in _sql_text(c.args[0]) and "ticket_history" in _sql_text(c.args[0])
        ]
        fields = {(r[1], r[3]) for r in history_rows}
        assert ("status", PARKED) in fields
        assert ("parked_reason", "pre-beta expansion") in fields
        conn.commit.assert_called_once()

    @patch("stompy_ticketing.service.time")
    def test_reopen_clears_parked_metadata_only(self, mock_time):
        mock_time.time.return_value = FIXED_TIME
        current = _row(
            status=PARKED,
            metadata=json.dumps({"kept": 1, "parked": {"reason": "x", "revisit_by": None, "parked_at": 1.0, "from_status": "backlog"}}),
        )
        updated = _row(status="backlog", metadata=json.dumps({"kept": 1}))
        conn, cur = _conn()
        cur.fetchone.side_effect = [current, updated]

        result = self.service.transition_ticket(conn, SCHEMA, 1, "backlog")

        assert result.status == "backlog"
        _, params = _executed(cur, "UPDATE")
        assert json.loads(params[3]) == {"kept": 1}

    @patch("stompy_ticketing.service.time")
    def test_ordinary_transition_leaves_metadata_untouched(self, mock_time):
        """Non-park moves must not rewrite metadata (a corrupt blob stays as-is)."""
        mock_time.time.return_value = FIXED_TIME
        current = _row(status="backlog", metadata="{not json")
        updated = _row(status="in_progress", metadata="{not json")
        conn, cur = _conn()
        cur.fetchone.side_effect = [current, updated]

        self.service.transition_ticket(conn, SCHEMA, 1, "in_progress")

        text, params = _executed(cur, "UPDATE")
        assert "metadata" not in text
        assert len(params) == 4

    @patch("stompy_ticketing.service.time")
    def test_stale_parked_key_on_a_non_parked_ticket_is_left_alone(self, mock_time):
        """Only LEAVING parked clears the key; an ordinary move never rewrites metadata."""
        mock_time.time.return_value = FIXED_TIME
        stale = json.dumps({"parked": {"reason": "old"}})
        conn, cur = _conn()
        cur.fetchone.side_effect = [_row(status="backlog", metadata=stale), _row(status="in_progress", metadata=stale)]

        self.service.transition_ticket(conn, SCHEMA, 1, "in_progress")

        text, _ = _executed(cur, "UPDATE")
        assert "metadata" not in text

    def test_park_args_on_a_non_park_move_are_refused_not_dropped(self):
        conn, cur = _conn()
        cur.fetchone.return_value = _row(status="backlog")

        with pytest.raises(ParkArgumentError, match="only apply to status='parked'"):
            self.service.transition_ticket(conn, SCHEMA, 1, "in_progress", reason="why")
        with pytest.raises(ParkArgumentError, match="only apply to status='parked'"):
            self.service.transition_ticket(conn, SCHEMA, 1, "in_progress", revisit_by="2026-12-01")
        conn.commit.assert_not_called()

    def test_non_string_revisit_by_is_a_park_argument_error_not_a_type_error(self):
        conn, cur = _conn()
        cur.fetchone.return_value = _row(status="backlog")

        with pytest.raises(ParkArgumentError, match="revisit_by"):
            self.service.transition_ticket(conn, SCHEMA, 1, PARKED, reason="r", revisit_by=20261201)

    def test_reason_has_a_length_cap(self):
        conn, cur = _conn()
        cur.fetchone.return_value = _row(status="backlog")

        with pytest.raises(ParkArgumentError, match="max 2000"):
            self.service.transition_ticket(conn, SCHEMA, 1, PARKED, reason="x" * 2001)

    @patch("stompy_ticketing.service.time")
    def test_park_keeps_unparseable_metadata_as_evidence(self, mock_time):
        mock_time.time.return_value = FIXED_TIME
        conn, cur = _conn()
        cur.fetchone.side_effect = [_row(status="backlog", metadata="{not json"), _row(status=PARKED)]

        self.service.transition_ticket(conn, SCHEMA, 1, PARKED, reason="r")

        _, params = _executed(cur, "UPDATE")
        written = json.loads(params[3])
        assert written["_unparseable_metadata"] == "{not json"
        assert written["parked"]["from_status"] == "backlog"


class TestBatchPark:
    def setup_method(self):
        self.service = TicketService()

    def test_batch_park_without_reason_fails_before_touching_rows(self):
        conn, cur = _conn()

        result = self.service.batch_transition(conn, SCHEMA, [1, 2], PARKED, confirm=True)

        assert result.succeeded == 0
        assert result.failed == 2
        assert "reason" in result.results[0].error
        cur.execute.assert_not_called()

    def test_batch_park_args_on_non_park_target_fail_up_front(self):
        conn, cur = _conn()

        result = self.service.batch_transition(conn, SCHEMA, [1, 2], "in_progress", reason="why")

        assert result.failed == 2
        assert "only apply to status='parked'" in result.results[0].error
        cur.execute.assert_not_called()

    def test_batch_park_passes_reason_and_revisit_to_each_transition(self):
        conn, cur = _conn()
        cur.fetchone.side_effect = [
            {"type": "task", "status": "backlog"},
            {"type": "bug", "status": "triage"},
        ]
        with patch.object(self.service, "transition_ticket") as transition:
            result = self.service.batch_transition(
                conn, SCHEMA, [1, 2], PARKED, confirm=True,
                changed_by="51", reason="6.6.19 park", revisit_by="2026-12-01",
            )

        assert result.succeeded == 2
        for call in transition.call_args_list:
            assert call.kwargs["reason"] == "6.6.19 park"
            assert call.kwargs["revisit_by"] == "2026-12-01"


class TestBoardHidesParked:
    def setup_method(self):
        self.service = TicketService()

    def test_board_excludes_parked_by_default(self):
        conn, cur = _conn()
        cur.fetchone.return_value = {"count": 0}
        self.service.board_view(conn, SCHEMA, view="summary")

        text, params = _executed(cur, "NOT IN")
        assert PARKED in params

    def test_board_type_filter_excludes_parked_with_that_types_terminals(self):
        conn, cur = _conn()
        cur.fetchone.return_value = {"count": 0}
        self.service.board_view(conn, SCHEMA, type_filter="bug", view="summary")

        text, params = _executed(cur, "NOT IN")
        assert PARKED in params and "wont_fix" in params and "done" not in params


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
        MockService.return_value = svc
        register_ticketing_tools(
            mcp_instance=mcp,
            get_db_func=db_ctx,
            check_project_func=MagicMock(return_value=None),
            get_project_func=MagicMock(return_value=SCHEMA),
        )
        return mcp._registered_tools["ticket"], svc


def _run(coro):
    return asyncio.run(coro)


def _parked_response():
    return TicketResponse(id=1, title="t", type="task", status=PARKED, priority="medium")


class TestMcpDoor:
    def test_archive_with_ticket_id_is_refused_loudly(self):
        ticket, svc = _register()

        parsed = json.loads(_run(ticket(action="archive", ticket_id=5, project=SCHEMA)))

        assert parsed["success"] is False
        assert parsed["error"] == "INVALID_PARAMS"
        assert "parked" in parsed["message"], "the refusal names the right tool for the job"
        svc.archive_stale_tickets.assert_not_called()

    def test_archive_with_ticket_ids_is_refused_loudly(self):
        ticket, svc = _register()

        parsed = json.loads(_run(ticket(action="archive", ticket_ids="5,6", project=SCHEMA)))

        assert parsed["error"] == "INVALID_PARAMS"
        svc.archive_stale_tickets.assert_not_called()

    def test_archive_without_ids_still_sweeps(self):
        ticket, svc = _register()
        svc.archive_stale_tickets.return_value = 3

        parsed = json.loads(_run(ticket(action="archive", project=SCHEMA)))

        assert parsed["count"] == 3

    def test_move_to_parked_passes_reason_and_revisit_by(self):
        ticket, svc = _register()
        svc.transition_ticket.return_value = _parked_response()

        _run(ticket(
            action="move", ticket_id=1, status=PARKED, project=SCHEMA,
            reason="pre-beta expansion", revisit_by="2026-12-01",
        ))

        kwargs = svc.transition_ticket.call_args.kwargs
        assert kwargs["reason"] == "pre-beta expansion"
        assert kwargs["revisit_by"] == "2026-12-01"

    def test_move_to_parked_without_reason_is_a_structured_error(self):
        ticket, svc = _register()
        svc.transition_ticket.side_effect = ParkArgumentError("parking requires a reason")

        parsed = json.loads(_run(ticket(action="move", ticket_id=1, status=PARKED, project=SCHEMA)))

        assert parsed["success"] is False
        assert parsed["error"] == "PARK_ARGUMENT"
        assert parsed["recovery"]["steps"]

    def test_batch_move_passes_reason_and_revisit_by(self):
        ticket, svc = _register()
        svc.batch_transition.return_value = MagicMock(model_dump=lambda: {"ok": True})

        _run(ticket(
            action="batch_move", ticket_ids="1,2", status=PARKED, confirm=True,
            project=SCHEMA, reason="6.6.19 park", revisit_by="2026-12-01",
        ))

        kwargs = svc.batch_transition.call_args.kwargs
        assert kwargs["reason"] == "6.6.19 park"
        assert kwargs["revisit_by"] == "2026-12-01"


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
class TestRestDoor:
    def test_bodies_accept_reason_and_revisit_by(self):
        assert TicketTransition(status=PARKED, reason="x", revisit_by="2026-12-01").reason == "x"
        assert BatchMoveRequest(ticket_ids=[1], status=PARKED, reason="x").reason == "x"

    @patch("stompy_ticketing.service.time")
    async def test_move_route_carries_reason_to_the_core(self, mock_time):
        mock_time.time.return_value = FIXED_TIME
        conn, cur = _conn()
        cur.fetchone.side_effect = [
            _row(status="backlog"),
            _row(status=PARKED, metadata=json.dumps({"parked": {"reason": "why", "revisit_by": None, "parked_at": FIXED_TIME}})),
        ]
        async with AsyncClient(transport=ASGITransport(app=_app(conn)), base_url="http://t") as client:
            response = await client.post(
                f"/projects/{SCHEMA}/tickets/1/move",
                json={"status": PARKED, "reason": "why"},
            )

        assert response.status_code == 200
        assert response.json()["status"] == PARKED
        _, params = _executed(cur, "UPDATE")
        assert json.loads(params[3])["parked"]["reason"] == "why"

    async def test_move_route_without_reason_is_422(self):
        conn, cur = _conn()
        cur.fetchone.return_value = _row(status="backlog")
        async with AsyncClient(transport=ASGITransport(app=_app(conn)), base_url="http://t") as client:
            response = await client.post(
                f"/projects/{SCHEMA}/tickets/1/move", json={"status": PARKED}
            )

        assert response.status_code == 422
        assert "reason" in response.json()["detail"]

    async def test_batch_move_route_carries_reason(self):
        conn, cur = _conn()
        cur.fetchone.side_effect = [{"type": "task", "status": "backlog"}]
        with patch.object(TicketService, "transition_ticket") as transition:
            async with AsyncClient(transport=ASGITransport(app=_app(conn)), base_url="http://t") as client:
                response = await client.post(
                    f"/projects/{SCHEMA}/tickets/batch/move",
                    json={"ticket_ids": [1], "status": PARKED, "confirm": True, "reason": "why"},
                )

        assert response.status_code == 200
        assert transition.call_args.kwargs["reason"] == "why"
