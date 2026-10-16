"""STOMPY-2880: duplicate link -> ALREADY_LINKED on every door (REST 409, not 500);
reverse of a symmetric (related) link is the same link; directional reverse unchanged."""

import json
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from psycopg2 import IntegrityError

from stompy_ticketing.api_routes import configure_routes, router
from stompy_ticketing.models import LinkType, TicketLinkCreate
from stompy_ticketing.service import LinkAlreadyExistsError, TicketService
from tests.test_cluster_c_fixes import _register, _run
from tests.test_service import _sql_to_str

NEW_ROW = {
    "id": 5, "source_id": 2, "target_id": 4, "link_type": "related", "created_at": 1.0,
    "source_title": "B", "source_status": "backlog",
    "target_title": "A", "target_status": "backlog",
}


class Cur:
    """reverse_id: id of an existing reverse row (same type) or None.
    unique_hit: INSERT raises UniqueViolation (same-direction duplicate)."""

    def __init__(self, reverse_id=None, unique_hit=False):
        self.reverse_id, self.unique_hit = reverse_id, unique_hit
        self.inserted, self._row = False, None

    def execute(self, query, params=None):
        s = _sql_to_str(query)
        if "INSERT" in s:
            if self.unique_hit:
                raise IntegrityError("dup")
            self.inserted = True
            self._row = dict(NEW_ROW)
        elif "ticket_links" in s and "SELECT" in s:
            if self.unique_hit and "link_type = 'blocks'" not in s:
                self._row = {"id": 77}
            else:
                self._row = {"id": self.reverse_id} if self.reverse_id else None
        else:
            self._row = {"title": "A", "status": "backlog"}

    def fetchone(self):
        return self._row

    def fetchall(self):
        return []


def _conn(**kw):
    conn = MagicMock()
    conn.cursor.return_value = Cur(**kw)
    return conn


def _add(conn, src, tgt, lt):
    return TicketService().add_link(conn, "s", src, TicketLinkCreate(target_id=tgt, link_type=lt))


def test_service_refuses_reverse_related_as_already_linked_2880():
    conn = _conn(reverse_id=9)
    with pytest.raises(LinkAlreadyExistsError) as e:
        _add(conn, 2, 4, LinkType.related)
    assert e.value.link_id == 9
    assert not conn.cursor.return_value.inserted


@pytest.mark.parametrize("lt", [LinkType.parent, LinkType.duplicate])
def test_service_directional_reverse_still_allowed_2880(lt):
    conn = _conn(reverse_id=9)
    assert _add(conn, 2, 4, lt).id == 5


def test_service_same_direction_duplicate_carries_existing_id_2880():
    with pytest.raises(LinkAlreadyExistsError) as e:
        _add(_conn(unique_hit=True), 4, 2, LinkType.related)
    assert e.value.link_id == 77


def test_mcp_reverse_related_is_already_linked_client_class_2880():
    tools, _ = _register(db_factory=lambda n: _conn(reverse_id=9))
    out = json.loads(_run(tools["ticket_link"](action="add", ticket_id=2, target_id=4, link_type="related")))
    assert out["error_type"] == "ALREADY_LINKED"


async def _post(conn, body, ticket=2):
    @contextmanager
    def get_db(project=None, require_write=True):
        yield conn

    configure_routes(get_db_func=get_db)
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.post(f"/projects/p/tickets/{ticket}/links", json=body)


@pytest.mark.asyncio
async def test_rest_same_direction_duplicate_is_409_not_500_2880():
    r = await _post(_conn(unique_hit=True), {"target_id": 4, "link_type": "related"})
    assert r.status_code == 409, r.text
    assert r.json()["detail"].startswith("ALREADY_LINKED:")
    assert "77" in r.json()["detail"]


@pytest.mark.asyncio
async def test_rest_reverse_related_is_409_2880():
    r = await _post(_conn(reverse_id=9), {"target_id": 4, "link_type": "related"})
    assert r.status_code == 409, r.text
    assert "ALREADY_LINKED" in r.text


@pytest.mark.asyncio
async def test_rest_directional_reverse_still_201_2880():
    r = await _post(_conn(reverse_id=9), {"target_id": 4, "link_type": "parent"})
    assert r.status_code == 201, r.text
