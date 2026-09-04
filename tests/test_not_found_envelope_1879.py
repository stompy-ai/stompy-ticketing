"""STOMPY-1879 item 2: ticket(...) NOT_FOUND paths (get/append/update/move/
close) gain the structured envelope (code, message, recovery.steps,
can_retry) instead of a bare `{"error": "Ticket N not found"}` — an agent
cannot write one handler for two shapes, so it wrote none. RED first:
written before mcp_tools.py's not-found sites called
stompy_ticketing.errors.not_found_error.
"""

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from stompy_ticketing.mcp_tools import register_ticketing_tools


def _make_mock_mcp():
    mock = MagicMock()
    registered = {}

    def tool_decorator():
        def decorator(func):
            registered[func.__name__] = func
            return func

        return decorator

    mock.tool = tool_decorator
    mock._registered_tools = registered
    return mock


def _register(service_attr, return_value=None):
    """Register `ticket` with TicketService.<service_attr> mocked to
    `return_value` (None simulates "not found")."""
    mcp = _make_mock_mcp()
    check_project = MagicMock(return_value=None)
    get_project = MagicMock(return_value="test_project")

    @contextmanager
    def db_ctx(project=None, require_write=True):
        yield MagicMock()

    with patch("stompy_ticketing.mcp_tools.TicketService") as MockService:
        mock_svc = MagicMock()
        getattr(mock_svc, service_attr).return_value = return_value
        MockService.return_value = mock_svc

        register_ticketing_tools(
            mcp_instance=mcp,
            get_db_func=db_ctx,
            check_project_func=check_project,
            get_project_func=get_project,
        )
        return mcp._registered_tools["ticket"]


def _run(coro):
    return asyncio.run(coro)


class TestTicketNotFoundEnvelope:
    def test_get_not_found_uses_structured_envelope(self):
        ticket_fn = _register("get_ticket", return_value=None)

        result = _run(ticket_fn(action="get", ticket_id=99999, project="test_project"))
        parsed = json.loads(result)

        assert parsed["success"] is False
        assert parsed["error"] == "NOT_FOUND"
        assert "99999" in parsed["message"]
        assert parsed["recovery"]["steps"]
        assert parsed["recovery"]["can_retry"] is True

    def test_close_not_found_uses_structured_envelope(self):
        ticket_fn = _register("close_ticket", return_value=None)

        result = _run(
            ticket_fn(action="close", ticket_id=99999, project="test_project")
        )
        parsed = json.loads(result)

        assert parsed["success"] is False
        assert parsed["error"] == "NOT_FOUND"
        assert "99999" in parsed["message"]
        assert "recovery" in parsed

    def test_move_not_found_uses_structured_envelope(self):
        ticket_fn = _register("transition_ticket", return_value=None)

        result = _run(
            ticket_fn(
                action="move", ticket_id=99999, status="done", project="test_project"
            )
        )
        parsed = json.loads(result)

        assert parsed["error"] == "NOT_FOUND"
        assert "recovery" in parsed

    def test_append_not_found_uses_structured_envelope(self):
        ticket_fn = _register("append_description", return_value=None)

        result = _run(
            ticket_fn(
                action="append", ticket_id=99999, description="x", project="test_project"
            )
        )
        parsed = json.loads(result)

        assert parsed["error"] == "NOT_FOUND"
        assert "recovery" in parsed

    def test_update_not_found_uses_structured_envelope(self):
        ticket_fn = _register("update_ticket", return_value=None)

        result = _run(
            ticket_fn(action="update", ticket_id=99999, title="x", project="test_project")
        )
        parsed = json.loads(result)

        assert parsed["error"] == "NOT_FOUND"
        assert "recovery" in parsed
