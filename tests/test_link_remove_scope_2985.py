"""STOMPY-2985: removing a link is scoped to the ticket it is removed through."""

import json
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from stompy_ticketing import api_routes
from stompy_ticketing.api_routes import configure_routes, router
from stompy_ticketing.service import TicketService
from tests.test_cluster_c_fixes import _register, _run
from tests.test_service import _sql_to_str


def _conn(deleted):
    conn = MagicMock()
    cur = conn.cursor.return_value
    cur.fetchone.return_value = {"id": 1} if deleted else None
    return conn, cur


def test_service_remove_link_binds_ticket_in_sql_2985():
    conn, cur = _conn(True)
    assert TicketService().remove_link(conn, "s", 3, ticket_id=7) is True
    query, params = cur.execute.call_args[0]
    assert "source_id = %s OR target_id = %s" in _sql_to_str(query)
    assert params == (3, 7, 7, 7)


def test_service_remove_context_link_binds_ticket_in_sql_2985():
    conn, cur = _conn(False)
    assert TicketService().remove_context_link(conn, "s", 3, ticket_id=7) is False
    query, params = cur.execute.call_args[0]
    assert "ticket_id = %s" in _sql_to_str(query)
    assert params == (3, 7, 7)


def test_service_without_ticket_id_is_unscoped_2985():
    conn, cur = _conn(True)
    TicketService().remove_link(conn, "s", 3)
    assert cur.execute.call_args[0][1] == (3, None, None, None)


async def _delete(monkeypatch, ticket_link_ok, context_ok):
    svc = MagicMock()
    svc.remove_link.return_value = ticket_link_ok
    svc.remove_context_link.return_value = context_ok
    monkeypatch.setattr(api_routes, "_service", svc)

    @contextmanager
    def get_db(project=None, require_write=True):
        yield MagicMock()

    configure_routes(get_db_func=get_db)
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return svc, await c.delete("/projects/p/tickets/5/links/3")


@pytest.mark.asyncio
async def test_rest_passes_path_ticket_id_and_falls_back_to_context_2985(monkeypatch):
    svc, r = await _delete(monkeypatch, False, True)
    assert r.status_code == 204
    assert svc.remove_link.call_args.kwargs == {"ticket_id": 5}
    assert svc.remove_context_link.call_args.kwargs == {"ticket_id": 5}


@pytest.mark.asyncio
async def test_rest_foreign_link_is_404_2985(monkeypatch):
    _, r = await _delete(monkeypatch, False, False)
    assert r.status_code == 404 and r.json() == {"detail": "Link 3 not found"}


def test_mcp_remove_passes_ticket_id_2985():
    conn = MagicMock()
    conn.cursor.return_value.fetchone.return_value = None
    tools, _ = _register(db_factory=lambda n: conn)
    out = json.loads(_run(tools["ticket_link"](action="remove", ticket_id=5, link_id=3)))
    assert out == {"error": "Link 3 not found"}
    for query, params in (c[0] for c in conn.cursor.return_value.execute.call_args_list):
        if "DELETE" in _sql_to_str(query):
            assert 5 in params
