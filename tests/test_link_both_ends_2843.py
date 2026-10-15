"""STOMPY-2843: a link A -> B (blocks) read from EITHER end names the
counterpart ticket's id, title and status plus the direction and role.

The fake cursor evaluates the service's SELECT against a two-ticket world:
it honours each `JOIN <schema>.tickets <alias> ON <alias>.id = tl.<col>` and
each `<alias>.title|status AS <name>` projection, so a join on the wrong end
produces the wrong title exactly as Postgres would.
"""

import re
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from stompy_ticketing.api_routes import configure_routes, router
from stompy_ticketing.service import TicketService
from tests.test_service import _sql_to_str

SCHEMA = "user_1"
TICKETS = {
    1: {"title": "Ticket A", "status": "in_progress"},
    2: {"title": "Ticket B", "status": "backlog"},
}
LINKS = [{"id": 7, "source_id": 1, "target_id": 2, "link_type": "blocks", "created_at": 1.0}]


class FakeLinkCursor:
    def __init__(self):
        self._rows = []
        self.sqls = []

    def execute(self, query, params=None):
        sql_text = _sql_to_str(query)
        self.sqls.append(sql_text)
        if "ticket_links" not in sql_text:
            self._rows = []
            return
        params = list(params or [])
        joins = dict(re.findall(r"JOIN \S+\.tickets (\w+) ON \w+\.id = tl\.(\w+)", sql_text))
        proj = re.findall(r"(\w+)\.(title|status) AS (\w+)", sql_text, flags=re.I)
        where = sql_text.split("WHERE", 1)[1] if "WHERE" in sql_text else ""
        ids = {int(p) for p in params}
        rows = []
        for link in LINKS:
            hit = ("tl.source_id" in where and link["source_id"] in ids) or (
                "tl.target_id" in where and link["target_id"] in ids
            )
            if not hit:
                continue
            row = dict(link)
            for alias, col, name in proj:
                row[name] = TICKETS[link[joins[alias]]][col]
            rows.append(row)
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


def _assert_end(link, viewed_id):
    other = 2 if viewed_id == 1 else 1
    d = link if isinstance(link, dict) else link.model_dump()
    assert (d["source_id"], d["target_id"]) == (1, 2)
    assert d["source_title"] == "Ticket A"
    assert d["target_title"] == "Ticket B"
    assert d["peer_id"] == other
    assert d["peer_title"] == TICKETS[other]["title"]
    assert d["peer_status"] == TICKETS[other]["status"]
    assert d["direction"] == ("outgoing" if viewed_id == 1 else "incoming")
    assert d["relation"] == ("blocks" if viewed_id == 1 else "blocked_by")


@pytest.mark.parametrize("viewed", [1, 2])
def test_get_links_for_ticket_reads_both_ends_2843(viewed):
    links = TicketService()._get_links_for_ticket(FakeLinkCursor(), SCHEMA, viewed)
    assert len(links) == 1
    _assert_end(links[0], viewed)


def test_bulk_links_read_both_ends_2843():
    out = TicketService()._get_links_for_tickets_bulk(FakeLinkCursor(), SCHEMA, [1, 2])
    assert len(out[1]) == 1 and len(out[2]) == 1
    _assert_end(out[1][0], 1)
    _assert_end(out[2][0], 2)


def test_bulk_links_for_target_only_page_2843():
    out = TicketService()._get_links_for_tickets_bulk(FakeLinkCursor(), SCHEMA, [2])
    _assert_end(out[2][0], 2)


def _app():
    cur = FakeLinkCursor()
    conn = MagicMock()
    conn.cursor.return_value = cur

    @contextmanager
    def get_db(project=None, require_write=True):
        yield conn

    configure_routes(get_db_func=get_db)
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("viewed", [1, 2])
async def test_rest_list_links_reads_both_ends_2843(viewed):
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://t") as c:
        r = await c.get(f"/projects/p/tickets/{viewed}/links")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body) == 1
    _assert_end(body[0], viewed)
