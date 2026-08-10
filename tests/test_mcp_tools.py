"""Tests for MCP tool schema resolution.

TDD: Verifies that MCP tools correctly resolve project names to schema
names using resolve_schema_func, especially for projects with hyphens.
"""

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

from toon import decode as toon_decode


def _parse(text):
    """Parse TOON or JSON response."""
    try:
        return toon_decode(text)
    except Exception:
        return json.loads(text)

import pytest

from stompy_ticketing.mcp_tools import register_ticketing_tools

FIXED_TIME = 1700000000.0
SCHEMA = "test_project"


def _make_mock_mcp():
    """Create a mock FastMCP that captures registered tools."""
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


def _make_ticket_row(
    id=1,
    title="Test ticket",
    description="Test description",
    type="task",
    status="backlog",
    priority="medium",
    assignee=None,
    tags=None,
    metadata=None,
    session_id="sess_123",
    created_at=FIXED_TIME,
    updated_at=FIXED_TIME,
    closed_at=None,
):
    """Create a mock ticket DB row tuple."""
    return (
        id, title, description, type, status, priority,
        assignee, json.dumps(tags) if tags else None,
        json.dumps(metadata) if metadata else None,
        session_id, created_at, updated_at, closed_at,
        "abc123",  # content_hash
    )


@contextmanager
def _mock_db_conn():
    """Context manager yielding a mock connection."""
    conn = MagicMock()
    cursor = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    yield conn


class TestSchemaResolution:
    """Tests for resolve_schema_func in MCP tools."""

    def _register_tools(self, resolve_schema_func=None):
        """Register tools with optional schema resolver and return them."""
        mcp = _make_mock_mcp()
        get_db = MagicMock()

        @contextmanager
        def db_ctx(project=None, require_write=True):
            conn = MagicMock()
            cursor = MagicMock()
            conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
            conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
            yield conn

        get_db.side_effect = db_ctx

        check_project = MagicMock(return_value=None)
        get_project = MagicMock(side_effect=lambda p=None: p or "default")

        register_ticketing_tools(
            mcp_instance=mcp,
            get_db_func=get_db,
            check_project_func=check_project,
            get_project_func=get_project,
            resolve_schema_func=resolve_schema_func,
        )
        return mcp._registered_tools, get_db

    def test_should_accept_resolve_schema_func_parameter(self):
        """register_ticketing_tools should accept resolve_schema_func."""
        resolver = MagicMock(return_value="resolved_schema")
        tools, _ = self._register_tools(resolve_schema_func=resolver)
        assert "ticket" in tools

    def test_should_use_resolve_schema_for_create(self):
        """ticket create should use resolve_schema_func to get schema name."""
        resolver = MagicMock(return_value="onboarding_test_feb21")
        tools, get_db = self._register_tools(resolve_schema_func=resolver)

        with patch("stompy_ticketing.mcp_tools.TicketService") as MockService:
            mock_svc = MagicMock()
            mock_result = MagicMock()
            mock_result.model_dump.return_value = {
                "id": 1, "title": "Test", "status": "backlog",
            }
            mock_svc.create_ticket.return_value = mock_result
            MockService.return_value = mock_svc

            # Re-register to pick up mocked service
            mcp = _make_mock_mcp()
            check_project = MagicMock(return_value=None)
            get_project = MagicMock(return_value="onboarding-test-feb21")

            @contextmanager
            def db_ctx(project=None, require_write=True):
                yield MagicMock()

            register_ticketing_tools(
                mcp_instance=mcp,
                get_db_func=db_ctx,
                check_project_func=check_project,
                get_project_func=get_project,
                resolve_schema_func=resolver,
            )

            ticket_fn = mcp._registered_tools["ticket"]
            result = asyncio.get_event_loop().run_until_complete(
                ticket_fn(action="create", title="Test", project="onboarding-test-feb21")
            )

            # Verify resolver was called with the project name
            resolver.assert_called_with("onboarding-test-feb21")
            # Verify service received the RESOLVED schema, not raw name
            mock_svc.create_ticket.assert_called_once()
            call_args = mock_svc.create_ticket.call_args
            assert call_args[0][1] == "onboarding_test_feb21"  # schema arg

    def test_should_fallback_to_project_name_without_resolver(self):
        """Without resolve_schema_func, schema should be the raw project name."""
        tools, _ = self._register_tools(resolve_schema_func=None)

        with patch("stompy_ticketing.mcp_tools.TicketService") as MockService:
            mock_svc = MagicMock()
            mock_result = MagicMock()
            mock_result.model_dump.return_value = {"id": 1}
            mock_svc.create_ticket.return_value = mock_result
            MockService.return_value = mock_svc

            mcp = _make_mock_mcp()
            check_project = MagicMock(return_value=None)
            get_project = MagicMock(return_value="simple_project")

            @contextmanager
            def db_ctx(project=None, require_write=True):
                yield MagicMock()

            register_ticketing_tools(
                mcp_instance=mcp,
                get_db_func=db_ctx,
                check_project_func=check_project,
                get_project_func=get_project,
            )

            ticket_fn = mcp._registered_tools["ticket"]
            asyncio.get_event_loop().run_until_complete(
                ticket_fn(action="create", title="Test", project="simple_project")
            )

            mock_svc.create_ticket.assert_called_once()
            call_args = mock_svc.create_ticket.call_args
            assert call_args[0][1] == "simple_project"

    def test_should_resolve_schema_for_list_action(self):
        """ticket list should also use resolve_schema_func."""
        resolver = MagicMock(return_value="resolved_schema")

        with patch("stompy_ticketing.mcp_tools.TicketService") as MockService:
            mock_svc = MagicMock()
            mock_svc.list_tickets.return_value = MagicMock(
                model_dump=MagicMock(return_value={"tickets": [], "total": 0})
            )
            MockService.return_value = mock_svc

            mcp = _make_mock_mcp()
            check_project = MagicMock(return_value=None)
            get_project = MagicMock(return_value="my-project")

            @contextmanager
            def db_ctx(project=None, require_write=True):
                yield MagicMock()

            register_ticketing_tools(
                mcp_instance=mcp,
                get_db_func=db_ctx,
                check_project_func=check_project,
                get_project_func=get_project,
                resolve_schema_func=resolver,
            )

            ticket_fn = mcp._registered_tools["ticket"]
            asyncio.get_event_loop().run_until_complete(
                ticket_fn(action="list", project="my-project")
            )

            resolver.assert_called_with("my-project")
            mock_svc.list_tickets.assert_called_once()
            call_args = mock_svc.list_tickets.call_args
            assert call_args[0][1] == "resolved_schema"

    def test_should_resolve_schema_for_board_view(self):
        """ticket_board should use resolve_schema_func."""
        resolver = MagicMock(return_value="resolved_schema")

        with patch("stompy_ticketing.mcp_tools.TicketService") as MockService:
            mock_svc = MagicMock()
            mock_svc.board_view.return_value = MagicMock(
                model_dump=MagicMock(return_value={"columns": []})
            )
            MockService.return_value = mock_svc

            mcp = _make_mock_mcp()
            check_project = MagicMock(return_value=None)
            get_project = MagicMock(return_value="my-project")

            @contextmanager
            def db_ctx(project=None, require_write=True):
                yield MagicMock()

            register_ticketing_tools(
                mcp_instance=mcp,
                get_db_func=db_ctx,
                check_project_func=check_project,
                get_project_func=get_project,
                resolve_schema_func=resolver,
            )

            board_fn = mcp._registered_tools["ticket_board"]
            asyncio.get_event_loop().run_until_complete(
                board_fn(project="my-project")
            )

            resolver.assert_called_with("my-project")
            mock_svc.board_view.assert_called_once()
            call_args = mock_svc.board_view.call_args
            assert call_args[0][1] == "resolved_schema"

    def test_should_resolve_schema_for_search(self):
        """ticket_search should use resolve_schema_func."""
        resolver = MagicMock(return_value="resolved_schema")

        with patch("stompy_ticketing.mcp_tools.TicketService") as MockService:
            mock_svc = MagicMock()
            mock_svc.search_tickets.return_value = MagicMock(
                model_dump=MagicMock(return_value={"results": []})
            )
            MockService.return_value = mock_svc

            mcp = _make_mock_mcp()
            check_project = MagicMock(return_value=None)
            get_project = MagicMock(return_value="my-project")

            @contextmanager
            def db_ctx(project=None, require_write=True):
                yield MagicMock()

            register_ticketing_tools(
                mcp_instance=mcp,
                get_db_func=db_ctx,
                check_project_func=check_project,
                get_project_func=get_project,
                resolve_schema_func=resolver,
            )

            search_fn = mcp._registered_tools["ticket_search"]
            asyncio.get_event_loop().run_until_complete(
                search_fn(query="test", project="my-project")
            )

            resolver.assert_called_with("my-project")
            mock_svc.search_tickets.assert_called_once()
            call_args = mock_svc.search_tickets.call_args
            assert call_args[0][1] == "resolved_schema"

    def test_should_resolve_schema_for_link(self):
        """ticket_link should use resolve_schema_func."""
        resolver = MagicMock(return_value="resolved_schema")

        with patch("stompy_ticketing.mcp_tools.TicketService") as MockService:
            mock_svc = MagicMock()
            mock_svc.list_links.return_value = []
            MockService.return_value = mock_svc

            mcp = _make_mock_mcp()
            check_project = MagicMock(return_value=None)
            get_project = MagicMock(return_value="my-project")

            @contextmanager
            def db_ctx(project=None, require_write=True):
                yield MagicMock()

            register_ticketing_tools(
                mcp_instance=mcp,
                get_db_func=db_ctx,
                check_project_func=check_project,
                get_project_func=get_project,
                resolve_schema_func=resolver,
            )

            link_fn = mcp._registered_tools["ticket_link"]
            asyncio.get_event_loop().run_until_complete(
                link_fn(action="list", ticket_id=1, project="my-project")
            )

            resolver.assert_called_with("my-project")
            mock_svc.list_links.assert_called_once()
            call_args = mock_svc.list_links.call_args
            assert call_args[0][1] == "resolved_schema"


class TestUpdateActionRejectsStatus:
    """Tests that ticket(action='update', status=...) returns an error."""

    def _register_tools_with_mock_service(self, mock_svc):
        """Register tools with a pre-configured mock TicketService."""
        mcp = _make_mock_mcp()
        check_project = MagicMock(return_value=None)
        get_project = MagicMock(return_value="test-project")

        @contextmanager
        def db_ctx(project=None, require_write=True):
            yield MagicMock()

        with patch("stompy_ticketing.mcp_tools.TicketService", return_value=mock_svc):
            register_ticketing_tools(
                mcp_instance=mcp,
                get_db_func=db_ctx,
                check_project_func=check_project,
                get_project_func=get_project,
            )

        return mcp._registered_tools

    def test_update_with_status_returns_error(self):
        """ticket(action='update', status='in_progress') should return error, not silently ignore."""
        mock_svc = MagicMock()
        tools = self._register_tools_with_mock_service(mock_svc)

        ticket_fn = tools["ticket"]
        raw = asyncio.get_event_loop().run_until_complete(
            ticket_fn(action="update", ticket_id=1, status="in_progress")
        )
        data = _parse(raw)

        assert "error" in data
        assert "move" in data["error"].lower()
        # Should NOT have called update_ticket
        mock_svc.update_ticket.assert_not_called()

    def test_update_without_status_proceeds_normally(self):
        """ticket(action='update', title='New') without status should work normally."""
        mock_svc = MagicMock()
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {"id": 1, "title": "New", "status": "backlog"}
        mock_svc.update_ticket.return_value = mock_result

        tools = self._register_tools_with_mock_service(mock_svc)
        ticket_fn = tools["ticket"]
        raw = asyncio.get_event_loop().run_until_complete(
            ticket_fn(action="update", ticket_id=1, title="New")
        )
        data = _parse(raw)

        assert "error" not in data
        mock_svc.update_ticket.assert_called_once()


class TestTicketListPagination:
    """Tests for limit/offset pagination in the ticket tool's list action."""

    def _register_tools_with_mock_service(self, mock_svc):
        """Register tools with a pre-configured mock TicketService."""
        mcp = _make_mock_mcp()
        check_project = MagicMock(return_value=None)
        get_project = MagicMock(return_value="test-project")

        @contextmanager
        def db_ctx(project=None, require_write=True):
            yield MagicMock()

        with patch("stompy_ticketing.mcp_tools.TicketService", return_value=mock_svc):
            register_ticketing_tools(
                mcp_instance=mcp,
                get_db_func=db_ctx,
                check_project_func=check_project,
                get_project_func=get_project,
            )

        return mcp._registered_tools

    def test_list_default_limit_is_20(self):
        """ticket(action='list') without limit should default to 20."""
        mock_svc = MagicMock()
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {
            "tickets": [], "total": 0, "limit": 20, "offset": 0, "has_more": False,
        }
        mock_svc.list_tickets.return_value = mock_result

        tools = self._register_tools_with_mock_service(mock_svc)
        ticket_fn = tools["ticket"]
        asyncio.get_event_loop().run_until_complete(
            ticket_fn(action="list")
        )

        mock_svc.list_tickets.assert_called_once()
        filters_arg = mock_svc.list_tickets.call_args[0][2]
        assert filters_arg.limit == 20
        assert filters_arg.offset == 0

    def test_list_custom_limit_and_offset(self):
        """ticket(action='list', limit=10, offset=30) should pass through."""
        mock_svc = MagicMock()
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {
            "tickets": [], "total": 50, "limit": 10, "offset": 30, "has_more": True,
        }
        mock_svc.list_tickets.return_value = mock_result

        tools = self._register_tools_with_mock_service(mock_svc)
        ticket_fn = tools["ticket"]
        asyncio.get_event_loop().run_until_complete(
            ticket_fn(action="list", limit=10, offset=30)
        )

        mock_svc.list_tickets.assert_called_once()
        filters_arg = mock_svc.list_tickets.call_args[0][2]
        assert filters_arg.limit == 10
        assert filters_arg.offset == 30

    def test_list_response_includes_pagination_metadata(self):
        """Response JSON should include limit, offset, has_more."""
        mock_svc = MagicMock()
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {
            "tickets": [{"id": 1, "title": "Test"}],
            "total": 54,
            "limit": 20,
            "offset": 0,
            "has_more": True,
            "by_status": {"backlog": 54},
            "by_type": {"task": 54},
        }
        mock_svc.list_tickets.return_value = mock_result

        tools = self._register_tools_with_mock_service(mock_svc)
        ticket_fn = tools["ticket"]
        raw = asyncio.get_event_loop().run_until_complete(
            ticket_fn(action="list")
        )
        data = _parse(raw)

        assert data["total"] == 54
        assert data["limit"] == 20
        assert data["offset"] == 0
        assert data["has_more"] is True

    def test_list_limit_capped_at_200(self):
        """Limit should not exceed 200 (TicketListFilters max)."""
        mock_svc = MagicMock()
        mock_result = MagicMock()
        mock_result.model_dump.return_value = {
            "tickets": [], "total": 0, "limit": 200, "offset": 0, "has_more": False,
        }
        mock_svc.list_tickets.return_value = mock_result

        tools = self._register_tools_with_mock_service(mock_svc)
        ticket_fn = tools["ticket"]
        raw = asyncio.get_event_loop().run_until_complete(
            ticket_fn(action="list", limit=500)
        )
        data = _parse(raw)

        # Should be capped to 200 by min() in the tool
        mock_svc.list_tickets.assert_called_once()
        filters_arg = mock_svc.list_tickets.call_args[0][2]
        assert filters_arg.limit <= 200
