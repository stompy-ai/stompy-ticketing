"""Tests for REST API routes using httpx TestClient.

Tests use a mock DB connection via configure_routes().
All test data is fixed and deterministic.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from stompy_ticketing.api_routes import configure_routes, router

# --------------------------------------------------------------------------- #
# Test fixtures                                                               #
# --------------------------------------------------------------------------- #

FIXED_TIME = 1700000000.0


def _make_ticket_row(**overrides):
    """Create a mock ticket row dict."""
    row = {
        "id": 1,
        "title": "Test ticket",
        "description": "A test ticket",
        "type": "task",
        "status": "backlog",
        "priority": "medium",
        "assignee": None,
        "tags": None,
        "metadata": None,
        "session_id": "sess_123",
        "created_at": FIXED_TIME,
        "updated_at": FIXED_TIME,
        "closed_at": None,
        "content_hash": "abc123",
        "content_tsvector": None,
    }
    row.update(overrides)
    return row


def _make_mock_conn(rows=None, fetchone_value=None):
    """Create a mock connection with cursor."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value = cur

    if rows is not None:
        cur.fetchall.return_value = rows

    if fetchone_value is not None:
        cur.fetchone.return_value = fetchone_value
    elif rows:
        cur.fetchone.return_value = rows[0]
    else:
        cur.fetchone.return_value = None

    return conn, cur


@contextmanager
def _mock_db_context(conn):
    """Context manager wrapper for mock connection."""
    yield conn


def _create_test_app(conn, cur):
    """Create a FastAPI test app with mock DB."""

    def get_db_func(project=None, require_write=True):
        return _mock_db_context(conn)

    configure_routes(get_db_func=get_db_func)

    app = FastAPI()
    app.include_router(router)
    return app


# --------------------------------------------------------------------------- #
# Tests                                                                       #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
class TestGetTicketAPI:
    async def test_get_existing_ticket(self):
        row = _make_ticket_row()
        conn, cur = _make_mock_conn(fetchone_value=row)
        cur.fetchall.return_value = []  # history + links
        app = _create_test_app(conn, cur)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/projects/test_project/tickets/1")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == 1
        assert data["title"] == "Test ticket"

    async def test_get_nonexistent_ticket_returns_404(self):
        conn, cur = _make_mock_conn(fetchone_value=None)
        app = _create_test_app(conn, cur)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/projects/test_project/tickets/999")

        assert response.status_code == 404


@pytest.mark.asyncio
class TestTransitionTicketAPI:
    @patch("stompy_ticketing.service.time")
    async def test_valid_transition(self, mock_time):
        mock_time.time.return_value = FIXED_TIME
        current = _make_ticket_row(status="backlog", type="task")
        updated = _make_ticket_row(status="in_progress", type="task")
        conn, cur = _make_mock_conn()
        cur.fetchone.side_effect = [current, updated]
        app = _create_test_app(conn, cur)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/projects/test_project/tickets/1/move",
                json={"status": "in_progress"},
            )

        assert response.status_code == 200
        assert response.json()["status"] == "in_progress"

    async def test_invalid_transition_returns_422(self):
        current = _make_ticket_row(status="backlog", type="task")
        conn, cur = _make_mock_conn(fetchone_value=current)
        app = _create_test_app(conn, cur)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/projects/test_project/tickets/1/move",
                json={"status": "resolved"},
            )

        assert response.status_code == 422
        assert "Cannot transition" in response.json()["detail"]


@pytest.mark.asyncio
class TestUpdateTicketAPI:
    @patch("stompy_ticketing.service.time")
    async def test_update_ticket(self, mock_time):
        mock_time.time.return_value = FIXED_TIME
        current = _make_ticket_row(title="Old title")
        updated = _make_ticket_row(title="New title")
        conn, cur = _make_mock_conn()
        cur.fetchone.side_effect = [current, updated]
        app = _create_test_app(conn, cur)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.put(
                "/projects/test_project/tickets/1",
                json={"title": "New title"},
            )

        assert response.status_code == 200
        assert response.json()["title"] == "New title"


@pytest.mark.asyncio
class TestBoardViewAPI:
    async def test_summary_board(self):
        conn, cur = _make_mock_conn()
        cur.fetchall.return_value = [
            {"status": "backlog", "count": 3},
            {"status": "in_progress", "count": 1},
        ]
        app = _create_test_app(conn, cur)

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get(
                "/projects/test_project/tickets/board?view=summary"
            )

        assert response.status_code == 200
        data = response.json()
        assert data["total"] == 4
        assert len(data["columns"]) == 2
