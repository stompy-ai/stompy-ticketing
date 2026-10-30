"""STOMPY-2961: the REST update body carries expected_updated_at and a stale
value is refused with the SAME Conflict the MCP action raises (409 CONFLICT
carrying the current updated_at), instead of being silently dropped."""

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from stompy_ticketing.api_routes import configure_routes, router
from stompy_ticketing.models import TicketUpdate
from stompy_ticketing.service import TicketService
from tests.test_concurrency_1579 import _row, _sqls


def _conn(fetches):
    cur = MagicMock()
    cur.fetchone.side_effect = fetches
    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def test_update_model_accepts_expected_updated_at_2961():
    assert TicketUpdate(description="x", expected_updated_at=100.0).expected_updated_at == 100.0
    assert TicketUpdate(description="x").expected_updated_at is None


def test_service_guards_with_the_value_carried_on_the_model_2961():
    conn, cur = _conn([_row(updated_at=200.0), None, {"updated_at": 200.0}])
    with pytest.raises(TicketService.Conflict) as e:
        TicketService().update_ticket(
            conn, "s", 7, TicketUpdate(description="mine", confirm=True, expected_updated_at=100.0)
        )
    assert e.value.expected_updated_at == 100.0
    assert e.value.current_updated_at == 200.0
    assert any("AND updated_at = %s" in q for q in _sqls(cur))


def test_explicit_kwarg_wins_over_model_value_2961():
    conn, cur = _conn([_row(updated_at=100.0), _row(description="mine")])
    TicketService().update_ticket(
        conn, "s", 7, TicketUpdate(description="mine", confirm=True, expected_updated_at=1.0),
        expected_updated_at=100.0,
    )
    upd = next(c for c in cur.execute.call_args_list if c.args and "UPDATE" in str(c.args[0]))
    assert 100.0 in tuple(upd.args[1]) and 1.0 not in tuple(upd.args[1])


async def _put(conn, body):
    @contextmanager
    def get_db(project=None, require_write=True):
        yield conn

    configure_routes(get_db_func=get_db)
    app = FastAPI()
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.put("/projects/p/tickets/7", json=body)


@pytest.mark.asyncio
async def test_rest_stale_expected_updated_at_is_409_conflict_2961():
    conn, _ = _conn([_row(updated_at=200.0), None, {"updated_at": 200.0}])
    r = await _put(conn, {"description": "mine", "confirm": True, "expected_updated_at": 100.0})
    assert r.status_code == 409, r.text
    d = r.json()["detail"]
    assert d["error_code"] == "CONFLICT"
    assert d["current_updated_at"] == 200.0
    assert d["expected_updated_at"] == 100.0


@pytest.mark.asyncio
async def test_rest_without_expected_updated_at_is_unchanged_2961():
    conn, _ = _conn([_row(updated_at=100.0), _row(description="mine")])
    r = await _put(conn, {"description": "mine", "confirm": True})
    assert r.status_code == 200, r.text
