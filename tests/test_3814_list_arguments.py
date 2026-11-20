"""STOMPY-3814: ticket accepts `tags` and `ticket_ids` as a list as well as a string.

Prod, 2026-11-20 20:33Z: ticket(action="create", tags=["ptt", "raha", ...]) was refused
twice by argument validation before the tool ran ("tags / Input should be a valid
string"). Reads return tags as a list, so agents send one; ticket_ids on the batch
actions has the same shape. These tests register the tools on a real FastMCP, so the
refusal layer itself is exercised (the suite's usual mock captures the raw function
and skips it).
"""

import asyncio
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from mcp.server.fastmcp import FastMCP

from stompy_ticketing.mcp_tools import register_ticketing_tools


def _call(svc, **args):
    mcp = FastMCP("t3814")

    @contextmanager
    def db_ctx(project=None, require_write=True):
        yield MagicMock()

    with patch("stompy_ticketing.mcp_tools.TicketService", return_value=svc):
        register_ticketing_tools(
            mcp_instance=mcp,
            get_db_func=db_ctx,
            check_project_func=MagicMock(return_value=None),
            get_project_func=MagicMock(return_value="test-project"),
        )
        tool = mcp._tool_manager.get_tool("ticket")
        try:
            return str(asyncio.run(tool.run({"project": "test-project", **args})))
        except Exception as exc:  # FastMCP raises on argument validation failure
            return f"REFUSED: {exc}"


def _svc():
    svc = MagicMock()
    svc.create_ticket.return_value = MagicMock(id=1)
    svc.list_tickets.return_value = []
    return svc


def test_create_takes_tags_as_a_list():
    svc = _svc()
    out = _call(svc, action="create", title="t", tags=["ptt", "raha"])
    assert not out.startswith("REFUSED"), out
    assert svc.create_ticket.call_args.args[2].tags == ["ptt", "raha"]


def test_list_filters_by_tags_given_as_a_list():
    svc = _svc()
    out = _call(svc, action="list", tags=["ptt", "raha"])
    assert not out.startswith("REFUSED"), out
    assert svc.list_tickets.call_args.args[2].tags == "ptt,raha"


def test_batch_ids_as_a_list_reach_the_batch():
    out = _call(_svc(), action="batch_close", ticket_ids=[11, "STOMPY-12"])
    assert not out.startswith("REFUSED"), out


# --- controls -----------------------------------------------------------------------------


def test_the_comma_string_still_works_control():
    svc = _svc()
    _call(svc, action="create", title="t", tags="ptt, raha")
    assert svc.create_ticket.call_args.args[2].tags == ["ptt", "raha"]


def test_a_number_is_not_a_tag_list_control():
    out = _call(_svc(), action="create", title="t", tags=5)
    assert out.startswith("REFUSED"), out
