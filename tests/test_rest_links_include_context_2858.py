"""STOMPY-2858: REST GET /{id}/links also returns context links (kind='context')."""

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from stompy_ticketing.api_routes import configure_routes, router
from tests.test_service import _sql_to_str

TICKET_ROW = {
    "id": 7, "source_id": 1, "target_id": 2, "link_type": "blocks", "created_at": 1.0,
    "source_title": "A", "source_status": "backlog",
    "target_title": "B", "target_status": "done",
}
CTX_ROW = {
    "id": 9, "ticket_id": 1, "context_label": "my_ctx", "context_version": "latest",
    "link_type": "relates_to", "created_at": 2.0,
    "ticket_title": "A", "ticket_status": "backlog",
}


class Cur:
    def __init__(self, ticket_rows, ctx_rows):
        self.t, self.c, self._rows = ticket_rows, ctx_rows, []

    def execute(self, query, params=None):
        s = _sql_to_str(query)
        self._rows = self.c if "ticket_context_links" in s else self.t if "ticket_links" in s else []

    def fetchall(self):
        return [dict(r) for r in self._rows]

    def fetchone(self):
        return dict(self._rows[0]) if self._rows else None


async def _get(ticket_rows, ctx_rows):
    conn = MagicMock()
    conn.cursor.return_value = Cur(ticket_rows, ctx_rows)

    @contextmanager
    def get_db(project=None, require_write=True):
        yield conn

    configure_routes(get_db_func=get_db)
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/projects/p/tickets/1/links")
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.asyncio
async def test_context_only_ticket_returns_one_context_row_2858():
    body = await _get([], [CTX_ROW])
    assert len(body) == 1
    row = body[0]
    assert row["kind"] == "context"
    assert row["id"] == 9 and row["ticket_id"] == 1
    assert row["context_label"] == "my_ctx" and row["context_version"] == "latest"
    assert row["link_type"] == "relates_to" and row["created_at"] == 2.0
    assert row["direction"] == "outgoing" and row["relation"] == "relates_to"
    assert row["source_id"] is None and row["target_id"] is None and row["peer_id"] is None


@pytest.mark.asyncio
async def test_mixed_ticket_returns_ticket_rows_then_context_row_2858():
    body = await _get([TICKET_ROW], [CTX_ROW])
    assert [r["kind"] for r in body] == ["ticket", "context"]
    assert [r["id"] for r in body] == [7, 9]


@pytest.mark.asyncio
async def test_ticket_rows_unchanged_apart_from_kind_2858():
    row = (await _get([TICKET_ROW], []))[0]
    assert row["kind"] == "ticket"
    expected = {
        "id": 7, "source_id": 1, "target_id": 2, "link_type": "blocks", "created_at": 1.0,
        "source_title": "A", "source_status": "backlog",
        "target_title": "B", "target_status": "done",
        "direction": "outgoing", "relation": "blocks",
        "peer_id": 2, "peer_title": "B", "peer_status": "done",
    }
    assert {k: row[k] for k in expected} == expected
