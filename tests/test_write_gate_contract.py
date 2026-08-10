"""STOMPY-1432: the plugin's side of the shared-project write gate.

The host's ``get_db_func`` enforces its write-role allowlist when a call
passes ``require_write=True`` (STOMPY-1423). This plugin therefore must
(a) pass the keyword EXPLICITLY on every acquisition — an omitted keyword
would silently take whatever the host defaults to — and (b) derive the
value from the action: mutations True, reads False. These tests pin both,
plus the action classification's totality against the tools' Literal
annotations, so a new action cannot ship unclassified.
"""

import ast
import inspect
from contextlib import contextmanager
from pathlib import Path
from typing import get_args, get_type_hints
from unittest.mock import MagicMock

import stompy_ticketing.api_routes as api_routes
import stompy_ticketing.mcp_tools as mcp_tools
from stompy_ticketing.mcp_tools import (
    TICKET_LINK_WRITE_ACTIONS,
    TICKET_WRITE_ACTIONS,
    register_ticketing_tools,
)

PKG = Path(mcp_tools.__file__).parent


def _calls_missing_require_write(module_path, callee_names):
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    missing = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else getattr(node.func, "attr", None)
            )
            if name in callee_names:
                kws = {k.arg for k in node.keywords}
                if None in kws or "require_write" not in kws:
                    missing.append(f"line {node.lineno}")
    return missing


def test_every_mcp_acquisition_passes_require_write():
    missing = _calls_missing_require_write(PKG / "mcp_tools.py", {"get_db_func"})
    assert not missing, f"mcp_tools get_db_func calls without explicit require_write: {missing}"


def test_every_rest_acquisition_passes_require_write():
    missing = _calls_missing_require_write(
        PKG / "api_routes.py", {"_get_db_for_project", "get_db_func"}
    )
    # configure_routes stores the callable without calling it; only real
    # acquisitions appear in the AST as Call nodes, so zero tolerance here.
    assert not missing, f"api_routes acquisitions without explicit require_write: {missing}"


def test_action_classification_is_total():
    """Every action a tool's Literal annotation admits is classified."""
    # The tools are closures created at registration; capture them.
    mcp = MagicMock()
    tools = {}

    def tool(*a, **k):
        def deco(fn):
            tools[fn.__name__] = fn
            return fn

        return deco

    mcp.tool = tool

    @contextmanager
    def db_ctx(project=None, require_write=True):
        yield MagicMock()

    register_ticketing_tools(
        mcp_instance=mcp,
        get_db_func=db_ctx,
        check_project_func=MagicMock(return_value=None),
        get_project_func=MagicMock(side_effect=lambda p=None: p or "default"),
    )

    ticket_actions = set(
        get_args(get_type_hints(tools["ticket"], include_extras=False)["action"])
    )
    link_actions = set(
        get_args(get_type_hints(tools["ticket_link"], include_extras=False)["action"])
    )

    assert TICKET_WRITE_ACTIONS <= ticket_actions
    ticket_reads = ticket_actions - TICKET_WRITE_ACTIONS
    assert ticket_reads == {"get", "list", "list_tags"}, (
        f"unclassified ticket actions treated as READ: {sorted(ticket_reads)} — "
        "add new mutating actions to TICKET_WRITE_ACTIONS"
    )

    assert TICKET_LINK_WRITE_ACTIONS <= link_actions
    assert link_actions - TICKET_LINK_WRITE_ACTIONS == {"list"}


class _SpyFactory:
    def __init__(self):
        self.calls = []

    @contextmanager
    def __call__(self, project=None, require_write=None):
        self.calls.append(require_write)
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = []
        cur.fetchone.return_value = None
        conn.cursor.return_value = cur
        yield conn


def _register_with_spy():
    spy = _SpyFactory()
    mcp = MagicMock()
    tools = {}

    def tool(*a, **k):
        def deco(fn):
            tools[fn.__name__] = fn
            return fn

        return deco

    mcp.tool = tool
    register_ticketing_tools(
        mcp_instance=mcp,
        get_db_func=spy,
        check_project_func=MagicMock(return_value=None),
        get_project_func=MagicMock(side_effect=lambda p=None: p or "default"),
    )
    return spy, tools


async def _invoke(fn, **kwargs):
    return await fn(**kwargs)


def test_read_and_write_actions_ask_for_the_right_gate():
    import asyncio

    spy, tools = _register_with_spy()

    asyncio.run(_invoke(tools["ticket"], action="list", project="p"))
    assert spy.calls[-1] is False, "ticket list must acquire read-only"

    asyncio.run(_invoke(tools["ticket"], action="create", title="t", project="p"))
    assert spy.calls[-1] is True, "ticket create must acquire as a write"

    asyncio.run(_invoke(tools["ticket_board"], project="p"))
    assert spy.calls[-1] is False, "ticket_board must acquire read-only"

    asyncio.run(_invoke(tools["ticket_search"], query="q", project="p"))
    assert spy.calls[-1] is False, "ticket_search must acquire read-only"

    asyncio.run(_invoke(tools["ticket_link"], action="list", ticket_id=1, project="p"))
    assert spy.calls[-1] is False, "ticket_link list must acquire read-only"
