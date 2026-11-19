"""Keep real structured ticket responses lossless without host dependencies."""

import json
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from toon import decode

from stompy_ticketing import api_routes, mcp_tools
from stompy_ticketing.models import (BoardColumn, BoardView,
                                     ContextLinkResponse, SearchResult,
                                     TicketCreate, TicketListResponse,
                                     TicketResponse)

FIXED_TIME = 1700000000.0
STEPS = ["First: do the thing", "Then: check the row"]


def parsed(text):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return decode(text)


@pytest.mark.parametrize(
    "items",
    [
        STEPS,
        ["comma, value", 'a "quote"'],
        ["line one\nline two", "next"],
        ["雪: 冬", "é"],
    ],
    ids=["colon", "comma-quote", "newline", "unicode-colon"],
)
def test_recovery_values_survive_real_encoder(items):
    payload = {"success": False, "recovery": {"can_retry": True, "steps": items}}
    assert parsed(mcp_tools._safe_json(payload)) == payload


def test_accepted_ticket_tags_survive_real_encoder():
    model = TicketCreate(title="codec triage", tags=["stage: beta", "check"])
    payload = {"title": model.title, "tags": model.tags}
    assert parsed(mcp_tools._safe_json(payload)) == payload


@pytest.mark.parametrize("bad", [{"recovery": {}}, {"recovery": {"can_retry": False}}])
def test_decodable_but_changed_output_uses_json(monkeypatch, bad):
    payload = {"recovery": {"can_retry": True, "steps": ["retry"]}}
    monkeypatch.setattr("toon.decode", lambda text: bad)
    assert json.loads(mcp_tools._safe_json(payload)) == payload


def test_encoder_exception_control_uses_json(monkeypatch):
    def broken(data):
        raise ValueError("injected encoder failure")

    monkeypatch.setattr("toon.encode", broken)
    payload = {"tags": ["beta", "check"]}
    assert json.loads(mcp_tools._safe_json(payload)) == payload


def test_ordinary_toon_control_stays_compact():
    payload = {
        "title": "codec triage",
        "tags": ["beta", "check"],
        "retry": False,
        "count": 0,
    }
    text = mcp_tools._safe_json(payload)
    assert not text.startswith("{")
    assert parsed(text) == payload


@pytest.fixture
def doors(monkeypatch):
    tools = {}
    mcp = MagicMock()
    mcp.tool.return_value = lambda fn: tools.setdefault(fn.__name__, fn)
    service = MagicMock()
    monkeypatch.setattr(mcp_tools, "TicketService", lambda: service)
    # Registration's host stamper is process-global; isolate it from other tests.
    monkeypatch.setattr(mcp_tools, "_stamp_urls_func", None)

    @contextmanager
    def db(project=None, require_write=True):
        yield MagicMock()

    mcp_tools.register_ticketing_tools(
        mcp,
        db,
        lambda project: None,
        lambda project: project,
    )
    return tools, service, db


def ticket():
    return TicketResponse(
        id=7,
        title="codec triage",
        type="task",
        status="backlog",
        priority="medium",
        tags=["stage: beta", "check"],
        created_at=FIXED_TIME,
        metadata={"recovery": {"can_retry": True, "steps": STEPS}},
    )


@pytest.mark.parametrize("door", ["get", "list", "search", "board"])
async def test_registered_mcp_ticket_shapes_keep_tags_and_recovery(doors, door):
    tools, service, _ = doors
    item = ticket()
    if door == "get":
        service.get_ticket.return_value = item
        result = await tools["ticket"](action="get", ticket_id=7, project="proj")
        actual = parsed(result)
    elif door == "board":
        service.board_view.return_value = BoardView(
            columns=[BoardColumn(status="backlog", count=1, tickets=[item])],
            total=1,
        )
        actual = parsed(await tools["ticket_board"](project="proj"))["columns"][0][
            "tickets"
        ][0]
    else:
        response = (
            TicketListResponse(tickets=[item], total=1)
            if door == "list"
            else SearchResult(tickets=[item], total=1, query="codec")
        )
        getattr(
            service, "list_tickets" if door == "list" else "search_tickets"
        ).return_value = response
        result = (
            await tools["ticket"](action="list", project="proj")
            if door == "list"
            else await tools["ticket_search"](query="codec", project="proj")
        )
        actual = parsed(result)["tickets"][0]
    assert actual["tags"] == item.tags
    assert actual["metadata"]["recovery"] == item.metadata["recovery"]
    assert actual["id"] == 7


async def test_registered_link_control_preserves_colon_labels(doors):
    tools, service, _ = doors
    service.list_links.return_value = []
    service.list_context_links_for_ticket.return_value = [
        ContextLinkResponse(
            id=3,
            ticket_id=7,
            context_label="step: one",
            context_version="v1",
            link_type="related",
        )
    ]
    actual = parsed(
        await tools["ticket_link"](action="list", ticket_id=7, project="proj")
    )
    assert actual["ticket_id"] == 7
    assert actual["context_links"][0]["context_label"] == "step: one"


async def test_rest_control_returns_the_same_values_without_toon(doors, monkeypatch):
    _, service, db = doors
    item = ticket()
    service.get_ticket.return_value = item
    monkeypatch.setattr(api_routes, "_get_db_for_project", db)
    monkeypatch.setattr(api_routes, "_resolve_schema", None)
    monkeypatch.setattr(api_routes, "_service", service)
    app = FastAPI()
    app.include_router(api_routes.router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        for _ in range(2):
            response = await client.get("/projects/proj/tickets/7")
            assert response.status_code == 200
            actual = response.json()
            assert actual["tags"] == item.tags
            assert actual["metadata"] == item.metadata
