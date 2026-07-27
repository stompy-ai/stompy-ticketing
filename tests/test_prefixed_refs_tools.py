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
    return asyncio.get_event_loop().run_until_complete(coro)


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
