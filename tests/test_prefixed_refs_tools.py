"""Prefixed-id behavior at the MCP tool layer (design 2026-07-27).

Covers: prefixed ref resolves + overrides project; explicit project conflict
refused; digit-string takes the int path; cross-project link explicitly
refused (not silently mangled); display_id decoration via the request-scoped
contextvar; registration stays backward compatible without the new kwargs.
"""

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import MagicMock

from stompy_ticketing.mcp_tools import register_ticketing_tools

FIXED_TIME = 1700000000.0


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


def _run(coro):
    return asyncio.run(coro)


def _register(resolve_prefix_func=None, get_prefix_func=None):
    mcp = _make_mock_mcp()
    seen = {"db_projects": []}

    @contextmanager
    def db_ctx(project=None):
        seen["db_projects"].append(project)
        conn = MagicMock()
        cursor = MagicMock()
        # get_ticket path: fetchone returns None → "not found" (fine for
        # routing assertions; we care about WHERE the call went)
        cursor.fetchone.return_value = None
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        yield conn

    register_ticketing_tools(
        mcp_instance=mcp,
        get_db_func=db_ctx,
        check_project_func=MagicMock(return_value=None),
        get_project_func=MagicMock(side_effect=lambda p=None: p or "default"),
        resolve_prefix_func=resolve_prefix_func,
        get_prefix_func=get_prefix_func,
    )
    return mcp._registered_tools, seen


def _resolver(prefix):
    return {"BUG": "bug_inbox", "STOMPY": "stompy"}.get(prefix)


class TestPrefixedRefRouting:
    def test_prefixed_ref_overrides_project(self):
        tools, seen = _register(resolve_prefix_func=_resolver)
        _run(tools["ticket"](action="get", ticket_id="BUG-188"))
        # The routing IS the assertion: the DB context was opened for the
        # project the prefix resolved to, not the (absent) project param.
        assert seen["db_projects"] == ["bug_inbox"]

    def test_explicit_project_conflict_is_refused(self):
        tools, seen = _register(resolve_prefix_func=_resolver)
        out = _run(tools["ticket"](action="get", ticket_id="BUG-188", project="stompy"))
        payload = json.loads(out)
        assert "belongs to project 'bug_inbox'" in payload["error"]
        assert seen["db_projects"] == []  # refused before any DB touch

    def test_digit_string_stays_in_current_project(self):
        tools, seen = _register(resolve_prefix_func=_resolver)
        _run(tools["ticket"](action="get", ticket_id="42", project="stompy"))
        assert seen["db_projects"] == ["stompy"]

    def test_unknown_prefix_errors_without_db_touch(self):
        tools, seen = _register(resolve_prefix_func=_resolver)
        out = _run(tools["ticket"](action="get", ticket_id="NOPE-1"))
        assert "Unknown ticket prefix" in json.loads(out)["error"]
        assert seen["db_projects"] == []

    def test_batch_refs_spanning_projects_refused(self):
        tools, seen = _register(resolve_prefix_func=_resolver)
        out = _run(tools["ticket"](
            action="batch_close", ticket_ids="BUG-1,STOMPY-2", confirm=True
        ))
        assert "span multiple projects" in json.loads(out)["error"]
        assert seen["db_projects"] == []  # refused before any DB touch


class TestCrossProjectLinkRefusal:
    def test_cross_project_link_explicitly_refused(self):
        tools, seen = _register(resolve_prefix_func=_resolver)
        out = _run(tools["ticket_link"](
            action="add", ticket_id="STOMPY-1", target_id="BUG-2", link_type="related"
        ))
        payload = json.loads(out)
        assert "Cross-project links are not supported yet" in payload["error"]
        assert seen["db_projects"] == []


class TestBackwardCompat:
    def test_registration_without_new_kwargs_still_works(self):
        # Hosts on the old contract must keep working (pinned-hash rollouts).
        mcp = _make_mock_mcp()

        @contextmanager
        def db_ctx(project=None):
            yield MagicMock()

        register_ticketing_tools(
            mcp_instance=mcp,
            get_db_func=db_ctx,
            check_project_func=MagicMock(return_value=None),
            get_project_func=MagicMock(side_effect=lambda p=None: p or "default"),
        )
        assert set(mcp._registered_tools) >= {"ticket", "ticket_link", "ticket_board", "ticket_search"}

    def test_prefixed_ref_without_resolver_gives_guidance(self):
        tools, seen = _register(resolve_prefix_func=None)
        out = _run(tools["ticket"](action="get", ticket_id="STOMPY-1"))
        assert "not supported by this host" in json.loads(out)["error"]


class TestValidationOrdering:
    """Review finding #5: project validation must run AFTER ref coercion —
    a host whose check REFUSES project=None (the real host does, post-#501)
    must still serve ticket(action='get', ticket_id='BUG-188')."""

    def test_prefixed_ref_survives_none_refusing_check(self):
        mcp = _make_mock_mcp()
        seen = {"db_projects": [], "checked": []}

        @contextmanager
        def db_ctx(project=None):
            seen["db_projects"].append(project)
            conn = MagicMock()
            cursor = MagicMock()
            cursor.fetchone.return_value = None
            conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
            conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
            yield conn

        def strict_check(project=None):
            seen["checked"].append(project)
            if not project:
                return json.dumps({"error": "INVALID_PROJECT: project required"})
            return None

        register_ticketing_tools(
            mcp_instance=mcp,
            get_db_func=db_ctx,
            check_project_func=strict_check,
            get_project_func=MagicMock(side_effect=lambda p=None: p or "default"),
            resolve_prefix_func=_resolver,
        )
        _run(mcp._registered_tools["ticket"](action="get", ticket_id="BUG-188"))
        # The check saw the ref-supplied project, not None.
        assert seen["checked"] == ["bug_inbox"]
        assert seen["db_projects"] == ["bug_inbox"]


class TestDisplayIdDecoration:
    def test_success_path_response_carries_display_id(self):
        from unittest.mock import patch

        from stompy_ticketing.models import TicketResponse

        tools, seen = _register(
            resolve_prefix_func=_resolver,
            get_prefix_func=lambda project: {"stompy": "STOMPY"}.get(project),
        )
        response = TicketResponse(
            id=42, title="T", description=None, type="task", status="backlog",
            priority="medium", assignee=None, tags=[], metadata=None,
            session_id=None, created_at=1700000000.0, updated_at=1700000000.0,
            closed_at=None,
        )
        with patch(
            "stompy_ticketing.mcp_tools.TicketService.get_ticket", return_value=response
        ):
            out = _run(tools["ticket"](action="get", ticket_id=42, project="stompy"))
        assert "STOMPY-42" in out
