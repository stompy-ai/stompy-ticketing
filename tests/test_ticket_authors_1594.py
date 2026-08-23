"""STOMPY-1594: tickets carry their author for the LLM.

tickets.created_by (str(internal_id), never the auth0 sub) is written on
create; ticket_history.changed_by carries the same identity on every MCP
write (it was NULL on all of them); the reader gets display names resolved
by the host. Both hooks are optional — an older host leaves NULLs as before.
"""

import asyncio
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from stompy_ticketing import mcp_tools
from stompy_ticketing.migrations import get_ticket_migrations
from stompy_ticketing.models import TicketCreate, TicketHistoryEntry, TicketResponse
from stompy_ticketing.service import TicketService

FIXED_TIME = 1700000000.0


class TestSchema:
    def test_created_by_migration_extends_the_block(self):
        ms = get_ticket_migrations(start_id=26)
        m = [x for x in ms if x["description"] == "add_tickets_created_by"]
        assert len(m) == 1 and m[0]["id"] == 31
        assert "ADD COLUMN IF NOT EXISTS created_by TEXT" in m[0]["spec"]["sql"]
        assert [x["id"] for x in ms] == sorted(x["id"] for x in ms)  # never renumbered


class TestServiceWritesTheFiler:
    def test_create_inserts_created_by(self):
        conn, cur = MagicMock(), MagicMock()
        conn.cursor.return_value = cur
        cur.fetchone.return_value = {
            "id": 1, "title": "t", "type": "task", "status": "backlog", "priority": "medium",
            "created_by": "51",
        }
        with patch("stompy_ticketing.service.time") as t:
            t.time.return_value = FIXED_TIME
            out = TicketService().create_ticket(
                conn, "proj", TicketCreate(title="t", type="task"), changed_by="51"
            )
        sql, params = cur.execute.call_args.args
        assert "created_by" in str(sql) and params[-1] == "51"
        assert out.created_by == "51"


def _mcp():
    tools = {}
    mcp = MagicMock()
    mcp.tool.return_value = lambda fn: tools.__setitem__(fn.__name__, fn) or fn
    return mcp, tools


@contextmanager
def _db(project=None, require_write=True):
    yield MagicMock()


def _register(actor=None, display=None):
    mcp, tools = _mcp()
    service = MagicMock()
    with patch.object(mcp_tools, "TicketService", return_value=service):
        mcp_tools.register_ticketing_tools(
            mcp_instance=mcp,
            get_db_func=_db,
            check_project_func=MagicMock(return_value=None),
            get_project_func=MagicMock(side_effect=lambda p=None: p or "proj"),
            actor_func=actor,
            display_actors_func=display,
        )
    return tools["ticket"], service


def _ticket(**kw):
    base = dict(id=7, title="t", type="task", status="backlog", priority="medium")
    base.update(kw)
    return TicketResponse(**base)


class TestMcpWritesCarryTheActor:
    def test_create_and_every_transition_write_the_actor(self):
        tool, service = _register(actor=lambda: "51")
        service.create_ticket.return_value = _ticket(created_by="51")
        service.transition_ticket.return_value = _ticket(status="in_progress")
        service.close_ticket.return_value = _ticket(status="done")
        service.append_description.return_value = _ticket()
        asyncio.run(tool(action="create", title="t", project="proj"))
        asyncio.run(tool(action="move", ticket_id=7, status="in_progress", project="proj"))
        asyncio.run(tool(action="close", ticket_id=7, project="proj"))
        asyncio.run(tool(action="append", ticket_id=7, description="more", project="proj"))
        for call in (
            service.create_ticket,
            service.transition_ticket,
            service.close_ticket,
            service.append_description,
        ):
            assert call.call_args.kwargs["changed_by"] == "51", call

    def test_no_actor_hook_leaves_null_as_before(self):
        tool, service = _register(actor=None)
        service.create_ticket.return_value = _ticket()
        asyncio.run(tool(action="create", title="t", project="proj"))
        assert service.create_ticket.call_args.kwargs["changed_by"] is None

    def test_a_raising_actor_hook_never_fails_the_write(self):
        def boom():
            raise RuntimeError("identity unavailable")

        tool, service = _register(actor=boom)
        service.create_ticket.return_value = _ticket()
        out = asyncio.run(tool(action="create", title="t", project="proj"))
        assert "created" in out
        assert service.create_ticket.call_args.kwargs["changed_by"] is None


class TestReaderSeesDisplayNames:
    def test_get_resolves_filer_and_history_actors(self):
        tool, service = _register(
            actor=lambda: "51",
            display=lambda ids: {"51": "Markus", "6846": "Jeroen (agent)"},
        )
        service.get_ticket.return_value = _ticket(
            created_by="51",
            history=[
                TicketHistoryEntry(id=1, field_name="status", changed_by="6846", changed_at=1.0),
                TicketHistoryEntry(id=2, field_name="status", changed_by=None, changed_at=2.0),
            ],
        )
        out = asyncio.run(tool(action="get", ticket_id=7, project="proj"))  # TOON text
        assert "created_by_display: Markus" in out
        assert "Jeroen (agent)" in out  # the history row's changed_by_display

    def test_resolver_failure_degrades_to_raw_ids(self):
        def boom(ids):
            raise RuntimeError("MAIN unreachable")

        tool, service = _register(actor=lambda: "51", display=boom)
        service.get_ticket.return_value = _ticket(created_by="51")
        out = asyncio.run(tool(action="get", ticket_id=7, project="proj"))
        assert ("created_by: 51" in out or 'created_by: "51"' in out)  # TOON quotes digits
        assert "created_by_display: null" in out
