"""ticket(get) on the MCP door ships each fact once (STOMPY-3293).

A ticket with six appended sections shipped its body twice: the description
holds every section and each description_appended history row carried the
same section again as new_value. Every ticket-link row carried
source_title/source_status AND target_title/target_status beside
peer_title/peer_status, so the ticket's own title rode every row and the
peer's rode it twice. Every context-link row carried the ticket's own
title and status again.

On the MCP get door:
  * a description_appended row keeps new_value_chars (the section's length)
    and the first APPENDED_HEAD_CHARS characters; the whole text is in the
    description;
  * a ticket-link row keeps ids, direction, relation and peer_*, without the
    endpoint title/status pairs;
  * a context-link row drops ticket_title / ticket_status.

The plugin REST detail (GET /projects/{name}/tickets/{id}) keeps the whole
shape. No web view reads its history or links (the web detail dialog renders
the board row), but it is the public REST record, so its shape stays as it was.
"""

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from stompy_ticketing.api_routes import configure_routes, router
from stompy_ticketing.mcp_tools import register_ticketing_tools
from stompy_ticketing.models import (
    ContextLinkResponse,
    TicketHistoryEntry,
    TicketLinkResponse,
    TicketResponse,
)

FIXED_TIME = 1700000000.0
SCHEMA = "test_project"
TITLE = "The ticket's own title, long enough to be worth not repeating"
#: ticket_projection.APPENDED_HEAD_CHARS, pinned: the head the ticket asked for.
APPENDED_HEAD_CHARS = 100
BASE = "The original body of the ticket, describing the problem in a paragraph."


def section(i):
    # A tail sentence unique per section: it must appear once in the payload.
    return (
        f"## Result {i}\n" + "A realistic appended report paragraph with measurements. " * 6
        + f"UNIQUE-TAIL-{i}."
    )


SECTIONS = [section(i) for i in range(1, 7)]


def record():
    """Six appended sections, a status change, three ticket links, five context links."""
    history = [
        TicketHistoryEntry(id=100 + i, field_name="description_appended", old_value=None,
                           new_value=s, changed_by="51", changed_at=FIXED_TIME + i)
        for i, s in enumerate(SECTIONS)
    ]
    history.append(TicketHistoryEntry(id=99, field_name="status", old_value="triage",
                                      new_value="confirmed", changed_by="51",
                                      changed_at=FIXED_TIME))
    links = []
    for link_id, (src, tgt, typ) in enumerate([(1, 2, "blocks"), (3, 1, "parent"), (1, 4, "related")]):
        outgoing = src == 1
        peer = tgt if outgoing else src
        links.append(TicketLinkResponse(
            id=link_id, source_id=src, target_id=tgt, link_type=typ, created_at=FIXED_TIME,
            source_title=TITLE if outgoing else f"Peer {peer}", source_status="confirmed" if outgoing else "backlog",
            target_title=f"Peer {peer}" if outgoing else TITLE, target_status="backlog" if outgoing else "confirmed",
            direction="outgoing" if outgoing else "incoming", relation=typ,
            peer_id=peer, peer_title=f"Peer {peer}", peer_status="backlog",
        ))
    context_links = [
        ContextLinkResponse(id=10 + i, ticket_id=1, context_label=f"label_{i}", context_version="latest",
                            link_type="references", created_at=FIXED_TIME,
                            ticket_title=TITLE, ticket_status="confirmed")
        for i in range(5)
    ]
    return TicketResponse(
        id=1, title=TITLE, description="\n\n".join([BASE, *SECTIONS]), type="bug",
        status="confirmed", priority="medium", created_by="51", created_at=FIXED_TIME,
        updated_at=FIXED_TIME, history=history, links=links, context_links=context_links,
    )


def _mock_mcp():
    registered = {}
    mock = MagicMock()

    def tool_decorator(*a, **kw):
        def wrap(fn):
            registered[fn.__name__] = fn
            return fn
        return wrap

    mock.tool = tool_decorator
    return mock, registered


def mcp_get():
    mcp, tools = _mock_mcp()

    @contextmanager
    def db_ctx(project=None, require_write=True):
        yield MagicMock()

    with patch("stompy_ticketing.mcp_tools.TicketService") as MockService:
        svc = MagicMock()
        svc.get_ticket.return_value = record()
        MockService.return_value = svc
        register_ticketing_tools(
            mcp_instance=mcp, get_db_func=db_ctx,
            check_project_func=MagicMock(return_value=None),
            get_project_func=MagicMock(return_value=SCHEMA),
        )
    text = asyncio.run(tools["ticket"](action="get", ticket_id=1, project=SCHEMA))
    return text, _parse(text)


def _parse(text):
    try:
        from toon import decode

        return decode(text)
    except Exception:
        return json.loads(text)


def test_every_appended_section_is_shipped_once():
    text, _ = mcp_get()
    for i in range(1, 7):
        assert text.count(f"UNIQUE-TAIL-{i}.") == 1, i


def test_an_appended_row_carries_its_length_and_its_head():
    _, out = mcp_get()
    rows = [h for h in out["history"] if h["field_name"] == "description_appended"]
    assert len(rows) == 6
    by_id = {h["id"]: h for h in rows}
    for i, s in enumerate(SECTIONS):
        h = by_id[100 + i]
        assert h["new_value_chars"] == len(s)
        assert h["new_value"].startswith(s[:APPENDED_HEAD_CHARS])
        assert len(h["new_value"]) <= APPENDED_HEAD_CHARS + 3


def test_other_history_rows_are_unchanged():
    _, out = mcp_get()
    (status,) = [h for h in out["history"] if h["field_name"] == "status"]
    assert (status["old_value"], status["new_value"]) == ("triage", "confirmed")
    assert "new_value_chars" not in status


def test_the_ticket_title_is_shipped_once():
    text, _ = mcp_get()
    assert text.count(TITLE) == 1


def test_link_rows_carry_the_peer_not_the_endpoints():
    _, out = mcp_get()
    links = out["links"]
    assert len(links) == 3
    for l in links:
        for gone in ("source_title", "source_status", "target_title", "target_status"):
            assert gone not in l, gone
        assert l["peer_title"] == f"Peer {l['peer_id']}" and l["peer_status"] == "backlog"
        assert {"source_id", "target_id", "direction", "relation"} <= set(l)


def test_context_link_rows_drop_the_tickets_own_title_and_status():
    _, out = mcp_get()
    ctx = out["context_links"]
    assert [c["context_label"] for c in ctx] == [f"label_{i}" for i in range(5)]
    for c in ctx:
        assert "ticket_title" not in c and "ticket_status" not in c


def test_the_description_is_whole():
    _, out = mcp_get()
    assert out["description"] == record().description


def test_rest_detail_keeps_the_whole_record():
    """Control: the REST door's shape does not move."""
    svc = MagicMock()
    svc.get_ticket.return_value = record()

    @contextmanager
    def ctx(project=None, require_write=True):
        yield MagicMock()

    app = FastAPI()
    configure_routes(get_db_func=ctx)
    app.include_router(router)

    async def go():
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            return await c.get(f"/projects/{SCHEMA}/tickets/1")

    with patch("stompy_ticketing.api_routes._service", svc):
        r = asyncio.run(go())
    assert r.status_code == 200, r.text
    body = r.json()
    appended = {h["id"]: h["new_value"] for h in body["history"]
                if h["field_name"] == "description_appended"}
    assert appended == {100 + i: s for i, s in enumerate(SECTIONS)}
    assert all(l["source_title"] and l["target_title"] for l in body["links"])
    assert all(c["ticket_title"] == TITLE for c in body["context_links"])
