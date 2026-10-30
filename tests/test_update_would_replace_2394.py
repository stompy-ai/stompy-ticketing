"""STOMPY-2394: an update whose description would REPLACE a non-empty stored
description it does not contain is refused, DESCRIPTION_WOULD_REPLACE, unless
the caller passes confirm=true.

Twice an agent meaning to add a note sent update(description=<the note>) and
wiped the whole ticket body (STOMPY-1788 on 2026-09-21, STOMPY-3169 on
2026-10-30). append (STOMPY-1579) is the verb for adding; update replaces, and
now says so before it does.

The guard lives in TicketService.update_ticket, the one path both doors share:
the MCP `ticket` tool passes confirm as a kwarg, the REST PUT carries it on the
TicketUpdate body (the same way it carries expected_updated_at, STOMPY-2961).
"""

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from toon import decode as toon_decode

from stompy_ticketing.api_routes import configure_routes, router
from stompy_ticketing.mcp_tools import register_ticketing_tools
from stompy_ticketing.models import TicketUpdate
from stompy_ticketing.service import TicketService
from tests.test_concurrency_1579 import _row, _sqls

STORED = "original body: the whole scope of the ticket"


def _conn(stored=STORED, written="new"):
    cur = MagicMock()
    cur.fetchone.side_effect = [_row(description=stored), _row(description=written)]
    cur.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def _wrote(cur):
    return any("UPDATE" in q for q in _sqls(cur))


def _parse(text):
    try:
        return toon_decode(text)
    except Exception:
        return json.loads(text)


# --------------------------------------------------------------------------- #
# The service: the shared path                                                #
# --------------------------------------------------------------------------- #


class TestTheServiceRefusesAReplace:
    def test_replace_of_a_non_empty_description_is_refused_2394(self):
        conn, cur = _conn()
        with pytest.raises(TicketService.DescriptionWouldReplace) as e:
            TicketService().update_ticket(conn, "s", 7, TicketUpdate(description="a note"))
        assert e.value.stored_length == len(STORED)
        assert not _wrote(cur), "a refusal that still issues the UPDATE is not a refusal"

    def test_the_refusal_names_the_length_and_both_ways_out_2394(self):
        conn, _ = _conn()
        with pytest.raises(TicketService.DescriptionWouldReplace) as e:
            TicketService().update_ticket(conn, "s", 7, TicketUpdate(description="a note"))
        msg = str(e.value)
        assert f"{len(STORED)} chars" in msg
        assert 'action="append"' in msg
        assert "confirm=true" in msg

    def test_a_replace_bundled_with_other_fields_writes_none_of_them_2394(self):
        conn, cur = _conn()
        with pytest.raises(TicketService.DescriptionWouldReplace):
            TicketService().update_ticket(
                conn, "s", 7, TicketUpdate(title="renamed", description="a note")
            )
        assert not _wrote(cur)


class TestTheControlsThatMustStayUnchanged:
    """If these go red, the guard refuses too much."""

    def test_confirm_kwarg_replaces_exactly_as_before_2394(self):
        conn, cur = _conn()
        out = TicketService().update_ticket(
            conn, "s", 7, TicketUpdate(description="a note"), confirm=True
        )
        assert out is not None and _wrote(cur)

    def test_confirm_on_the_model_replaces_exactly_as_before_2394(self):
        conn, cur = _conn()
        TicketService().update_ticket(conn, "s", 7, TicketUpdate(description="a note", confirm=True))
        assert _wrote(cur)

    def test_an_update_that_extends_the_stored_text_proceeds_2394(self):
        conn, cur = _conn()
        TicketService().update_ticket(
            conn, "s", 7, TicketUpdate(description=STORED + "\n\nresult: done")
        )
        assert _wrote(cur)

    @pytest.mark.parametrize("stored", ["", None, "   \n"], ids=["empty", "null", "blank"])
    def test_an_empty_stored_description_is_written_2394(self, stored):
        conn, cur = _conn(stored=stored)
        TicketService().update_ticket(conn, "s", 7, TicketUpdate(description="first body"))
        assert _wrote(cur)

    @pytest.mark.parametrize(
        "data",
        [
            TicketUpdate(title="renamed"),
            TicketUpdate(priority="high"),
            TicketUpdate(tags=["a", "b"]),
            TicketUpdate(assignee="Astra"),
        ],
        ids=["title", "priority", "tags", "assignee"],
    )
    def test_updates_of_other_fields_are_unaffected_2394(self, data):
        conn, cur = _conn()
        TicketService().update_ticket(conn, "s", 7, data)
        assert _wrote(cur)


# --------------------------------------------------------------------------- #
# Door 1: the MCP ticket tool                                                 #
# --------------------------------------------------------------------------- #


def _ticket_tool(conn):
    mcp = MagicMock()
    registered = {}

    def tool_decorator():
        def decorator(func):
            registered[func.__name__] = func
            return func
        return decorator

    mcp.tool = tool_decorator

    @contextmanager
    def db_ctx(project=None, require_write=True):
        yield conn

    with patch("stompy_ticketing.mcp_tools.TicketService", return_value=TicketService()):
        register_ticketing_tools(
            mcp_instance=mcp,
            get_db_func=db_ctx,
            check_project_func=MagicMock(return_value=None),
            get_project_func=MagicMock(return_value="test-project"),
        )
    return registered["ticket"]


class TestTheMcpDoor:
    def test_mcp_update_replace_is_refused_description_would_replace_2394(self):
        conn, cur = _conn()
        data = _parse(asyncio.run(_ticket_tool(conn)(action="update", ticket_id=7, description="a note")))
        assert data["error"] == "DESCRIPTION_WOULD_REPLACE"
        assert data["stored_length"] == len(STORED)
        assert f"{len(STORED)} chars" in data["message"]
        assert 'action="append"' in data["message"] and "confirm=true" in data["message"]
        assert not _wrote(cur)

    def test_mcp_update_confirm_true_replaces_2394(self):
        conn, cur = _conn(written="a note")
        data = _parse(asyncio.run(
            _ticket_tool(conn)(action="update", ticket_id=7, description="a note", confirm=True)
        ))
        assert data.get("status") == "updated", data
        assert _wrote(cur)

    def test_mcp_update_title_only_is_unaffected_2394(self):
        conn, cur = _conn()
        data = _parse(asyncio.run(_ticket_tool(conn)(action="update", ticket_id=7, title="renamed")))
        assert data.get("status") == "updated", data

    def test_the_tool_description_states_replace_and_append_2394(self):
        doc = _ticket_tool(MagicMock()).__doc__
        assert "REPLACES" in doc
        assert "DESCRIPTION_WOULD_REPLACE" in doc
        assert "confirm=True" in doc


# --------------------------------------------------------------------------- #
# Door 2: REST PUT /projects/{name}/tickets/{id}                              #
# --------------------------------------------------------------------------- #


async def _put(conn, body):
    @contextmanager
    def get_db(project=None, require_write=True):
        yield conn

    configure_routes(get_db_func=get_db)
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.put("/projects/p/tickets/7", json=body)


class TestTheRestDoor:
    def test_rest_put_replace_is_409_description_would_replace_2394(self):
        conn, cur = _conn()
        r = asyncio.run(_put(conn, {"description": "a note"}))
        assert r.status_code == 409, r.text
        d = r.json()["detail"]
        assert d["error_code"] == "DESCRIPTION_WOULD_REPLACE"
        assert d["stored_length"] == len(STORED)
        assert "confirm" in d["message"]
        assert not _wrote(cur)

    def test_rest_put_confirm_true_in_the_body_replaces_2394(self):
        conn, cur = _conn(written="a note")
        r = asyncio.run(_put(conn, {"description": "a note", "confirm": True}))
        assert r.status_code == 200, r.text
        assert _wrote(cur)

    def test_rest_put_extend_is_unaffected_2394(self):
        conn, cur = _conn()
        r = asyncio.run(_put(conn, {"description": STORED + "\n\nmore"}))
        assert r.status_code == 200, r.text
