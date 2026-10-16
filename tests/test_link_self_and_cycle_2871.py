"""STOMPY-2871: ticket_link refuses a self-link and a direct blocks 2-cycle.

One core (TicketService.add_link), three doors: service, MCP, REST.
"""

import json
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from stompy_ticketing.api_routes import configure_routes, router
from stompy_ticketing.models import LinkType, TicketLinkCreate
from stompy_ticketing.service import LinkRefusedError, TicketService
from tests.test_cluster_c_fixes import _register, _run
from tests.test_service import _sql_to_str

NEW_ROW = {
    "id": 5, "source_id": 2, "target_id": 1, "link_type": "blocks", "created_at": 1.0,
    "source_title": "B", "source_status": "backlog",
    "target_title": "A", "target_status": "backlog",
}


class Cur:
    """Fake cursor. reverse_exists answers the reverse-blocks lookup."""

    def __init__(self, reverse_exists):
        self.reverse_exists, self.inserted, self._row = reverse_exists, False, None

    def execute(self, query, params=None):
        s = _sql_to_str(query)
        if "INSERT" in s:
            self.inserted = True
            self._row = dict(NEW_ROW)
        elif "ticket_links" in s and "SELECT" in s:
            self._row = {"id": 1} if self.reverse_exists else None
        else:
            self._row = {"title": "A", "status": "backlog"}

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


def _conn(reverse_exists=False):
    conn = MagicMock()
    conn.cursor.return_value = Cur(reverse_exists)
    return conn


def _add(conn, src, tgt, lt):
    return TicketService().add_link(conn, "s", src, TicketLinkCreate(target_id=tgt, link_type=lt))


def test_service_refuses_self_link_2871():
    conn = _conn()
    with pytest.raises(LinkRefusedError) as e:
        _add(conn, 1, 1, LinkType.related)
    assert e.value.code == "SELF_LINK"
    assert not conn.cursor.return_value.inserted


def test_service_refuses_blocks_two_cycle_naming_both_ids_2871():
    conn = _conn(reverse_exists=True)
    with pytest.raises(LinkRefusedError) as e:
        _add(conn, 2, 1, LinkType.blocks)
    assert e.value.code == "LINK_CYCLE"
    assert "#1" in str(e.value) and "#2" in str(e.value)
    assert not conn.cursor.return_value.inserted


def test_service_allows_blocks_without_reverse_2871():
    conn = _conn(reverse_exists=False)
    assert _add(conn, 2, 1, LinkType.blocks).id == 5


def test_link_refused_is_value_error_2871():
    assert issubclass(LinkRefusedError, ValueError)


@pytest.mark.parametrize(
    "args,code",
    [
        (dict(ticket_id=1, target_id=1, link_type="related"), "SELF_LINK"),
        (dict(ticket_id=2, target_id=1, link_type="blocks"), "LINK_CYCLE"),
    ],
)
def test_mcp_refusal_is_a_client_refusal_with_code_2871(args, code):
    tools, _ = _register(db_factory=lambda n: _conn(reverse_exists=True))
    out = json.loads(_run(tools["ticket_link"](action="add", **args)))
    # host classifies error_type in {InvalidTransition, ValueError, ALREADY_LINKED} as client
    assert out["error_type"] == "ValueError"
    assert out["code"] == code
    assert "status" not in out


@pytest.mark.parametrize(
    "body,reverse,code",
    [
        ({"target_id": 1, "link_type": "related"}, False, "SELF_LINK"),
        ({"target_id": 2, "link_type": "blocks"}, True, "LINK_CYCLE"),
    ],
)
@pytest.mark.asyncio
async def test_rest_refusal_is_422_with_code_2871(body, reverse, code):
    conn = _conn(reverse_exists=reverse)

    @contextmanager
    def get_db(project=None, require_write=True):
        yield conn

    configure_routes(get_db_func=get_db)
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/projects/p/tickets/1/links", json=body)
    assert r.status_code == 422, r.text
    assert code in r.text
