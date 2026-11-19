"""STOMPY-3721: a long ticket never comes back whole unless asked for.

The rename epic's tickets reached 70-160k characters through appended
hand-offs. `get` ignored fields="card", and `append` echoed the full updated
ticket, so an append that SUCCEEDED came back as an oversized-result error;
an agent that believed the error would append a duplicate.

- append answers with a receipt: the card projection plus appended_chars and
  description_chars, never the body;
- get with fields="card" answers the card projection plus description_chars;
- get without fields keeps the full record (the documented contract).
"""

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from stompy_ticketing import mcp_tools
from stompy_ticketing.models import TicketResponse
from tests.tool_response import parse_tool_response

BODY = "x" * 50_000


def _mcp():
    tools = {}
    mcp = MagicMock()
    mcp.tool.return_value = lambda fn: tools.__setitem__(fn.__name__, fn) or fn
    return mcp, tools


@contextmanager
def _db(project=None, require_write=True):
    yield MagicMock()


def _register():
    mcp, tools = _mcp()
    service = MagicMock()
    with patch.object(mcp_tools, "TicketService", return_value=service):
        mcp_tools.register_ticketing_tools(
            mcp_instance=mcp,
            get_db_func=_db,
            check_project_func=MagicMock(return_value=None),
            get_project_func=MagicMock(side_effect=lambda p=None: p or "proj"),
        )
    return tools["ticket"], service


def _long_ticket():
    return TicketResponse(
        id=7, title="t", type="task", status="backlog", priority="medium", description=BODY
    )


def _call(tool, **kw):
    # The door encodes TOON; JSON here reads the same payload shape.
    with patch.object(mcp_tools, "_toon_encode", json.dumps):
        return parse_tool_response(asyncio.run(tool(project="proj", **kw)))


def test_append_answers_a_receipt_without_the_body():
    tool, service = _register()
    service.append_description.return_value = _long_ticket()
    out = _call(tool, action="append", ticket_id=7, description="a note")
    assert out["action"] == "appended"
    ticket = out["ticket"]
    assert ticket["id"] == 7
    assert ticket.get("description") is None
    assert ticket["appended_chars"] == len("a note")
    assert ticket["description_chars"] == len(BODY)
    assert len(json.dumps(out)) < 5_000


def test_get_with_card_fields_answers_the_card():
    tool, service = _register()
    service.get_ticket.return_value = _long_ticket()
    out = _call(tool, action="get", ticket_id=7, fields="card")
    assert out.get("description") is None
    assert out["description_chars"] == len(BODY)
    assert out["description_preview"] and len(out["description_preview"]) < 1_000
    assert len(json.dumps(out)) < 5_000


def test_get_without_fields_still_returns_the_full_record():
    tool, service = _register()
    service.get_ticket.return_value = _long_ticket()
    out = _call(tool, action="get", ticket_id=7)
    assert out["description"] == BODY
