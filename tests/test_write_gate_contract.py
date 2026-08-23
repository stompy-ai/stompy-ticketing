"""STOMPY-1432: the plugin's side of the shared-project write gate.

The host's ``get_db_func`` enforces its write-role allowlist when a call
passes ``require_write=True`` (STOMPY-1423). This plugin therefore must
(a) pass the keyword EXPLICITLY on every acquisition — an omitted keyword
would silently take whatever the host defaults to — and (b) derive the
value from the action / HTTP method: mutations True, reads False.

Three layers, because each catches what the others cannot (review of #24):
  presence  — AST: no acquisition may omit the keyword
  value     — AST: every REST route's literal matches its HTTP method, and
              the MCP tools acquire per a table hardcoded HERE, not derived
              from the constants under test (a derived table is vacuous)
  totality  — every action a tool's Literal admits is classified, and the
              runtime check is fail-CLOSED (unclassified acts as a write)
"""

import ast
import asyncio
from contextlib import contextmanager
from pathlib import Path
from typing import get_args, get_type_hints
from unittest.mock import MagicMock

import stompy_ticketing.mcp_tools as mcp_tools
from stompy_ticketing.mcp_tools import (
    TICKET_LINK_READ_ACTIONS,
    TICKET_LINK_WRITE_ACTIONS,
    TICKET_READ_ACTIONS,
    TICKET_WRITE_ACTIONS,
    register_ticketing_tools,
)

PKG = Path(mcp_tools.__file__).parent

# The REST contract, hardcoded: route function -> is-a-write. Derived from
# HTTP semantics, NOT from the source under test, so a wrong literal in
# api_routes.py fails here instead of being mirrored.
REST_ROUTE_WRITES = {
    "board_view": False,
    "search_tickets": False,
    "archive_tickets": True,
    "batch_move": True,
    "batch_close": True,
    "list_tags": False,
    "get_ticket": False,
    "update_ticket": True,
    "transition_ticket": True,
    "add_link": True,
    "list_links": False,
    "remove_link": True,
}


def _acquisitions(module_name, callee_names):
    """[(enclosing_function, lineno, require_write_node_or_None)] for every
    DB acquisition in the module. Module-level calls report as '<module>'."""
    tree = ast.parse((PKG / module_name).read_text(encoding="utf-8"))
    out = []

    def visit(node, current):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            current = node.name
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call):
                name = (
                    child.func.id
                    if isinstance(child.func, ast.Name)
                    else getattr(child.func, "attr", None)
                )
                if name in callee_names:
                    kw = {k.arg: k.value for k in child.keywords}
                    out.append((current or "<module>", child.lineno, kw))
            visit(child, current)

    visit(tree, None)
    return out


def _missing_keyword(acqs):
    return [
        f"{fn} line {ln}"
        for fn, ln, kw in acqs
        if None in kw or "require_write" not in kw
    ]


def test_every_mcp_acquisition_passes_require_write():
    assert not _missing_keyword(
        _acquisitions("mcp_tools.py", {"get_db_func"})
    ), "mcp_tools acquisitions without an explicit require_write"


def test_every_rest_acquisition_passes_require_write():
    assert not _missing_keyword(
        _acquisitions("api_routes.py", {"_get_db_for_project", "get_db_func"})
    ), "api_routes acquisitions without an explicit require_write"


def test_rest_route_gate_values_match_http_semantics():
    """Presence is not enough: a write route passing False would satisfy the
    presence test while re-opening the exact hole this PR closes."""
    acqs = _acquisitions("api_routes.py", {"_get_db_for_project"})
    seen, wrong = set(), []
    for fn, ln, kw in acqs:
        node = kw.get("require_write")
        if fn not in REST_ROUTE_WRITES:
            wrong.append(f"{fn} line {ln}: route not in the pinned contract table")
            continue
        seen.add(fn)
        expected = REST_ROUTE_WRITES[fn]
        if not (isinstance(node, ast.Constant) and node.value is expected):
            got = getattr(node, "value", ast.dump(node) if node else None)
            wrong.append(f"{fn} line {ln}: expected require_write={expected}, got {got}")
    assert not wrong, wrong
    missing = set(REST_ROUTE_WRITES) - seen
    assert not missing, f"pinned routes that no longer acquire a connection: {sorted(missing)}"


def _tools():
    """Register the tools against a spy factory; return (tools, spy_calls)."""
    calls = []

    @contextmanager
    def spy(project=None, require_write=None):
        calls.append(require_write)
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchall.return_value = []
        cur.fetchone.return_value = None
        conn.cursor.return_value = cur
        yield conn

    registered = {}
    mcp = MagicMock()

    def tool(*a, **k):
        def deco(fn):
            registered[fn.__name__] = fn
            return fn

        return deco

    mcp.tool = tool
    register_ticketing_tools(
        mcp_instance=mcp,
        get_db_func=spy,
        check_project_func=MagicMock(return_value=None),
        get_project_func=MagicMock(side_effect=lambda p=None: p or "default"),
    )
    return registered, calls


def test_action_classification_is_total_and_disjoint():
    """Every action the Literals admit is classified exactly once. A new
    action that lands in neither set acquires as a WRITE (fail closed), but
    this test makes the omission loud rather than silently restrictive."""
    tools, _ = _tools()
    ticket_actions = set(get_args(get_type_hints(tools["ticket"])["action"]))
    link_actions = set(get_args(get_type_hints(tools["ticket_link"])["action"]))

    assert not (TICKET_READ_ACTIONS & TICKET_WRITE_ACTIONS)
    assert not (TICKET_LINK_READ_ACTIONS & TICKET_LINK_WRITE_ACTIONS)

    unclassified = ticket_actions - TICKET_READ_ACTIONS - TICKET_WRITE_ACTIONS
    assert not unclassified, (
        f"unclassified ticket actions: {sorted(unclassified)} — add each to "
        "TICKET_READ_ACTIONS or TICKET_WRITE_ACTIONS. Until then they acquire "
        "as writes (fail closed), so reads would be refused for viewers."
    )
    unclassified_links = link_actions - TICKET_LINK_READ_ACTIONS - TICKET_LINK_WRITE_ACTIONS
    assert not unclassified_links, f"unclassified ticket_link actions: {sorted(unclassified_links)}"

    # No stale entries either: a classified action that no longer exists
    # hides a rename behind a green suite.
    assert (TICKET_READ_ACTIONS | TICKET_WRITE_ACTIONS) <= ticket_actions
    assert (TICKET_LINK_READ_ACTIONS | TICKET_LINK_WRITE_ACTIONS) <= link_actions


# (action kwargs, expected require_write) — hardcoded, not derived.
TICKET_CASES = [
    ({"action": "create", "title": "t"}, True),
    ({"action": "update", "ticket_id": 1, "title": "t"}, True),
    ({"action": "move", "ticket_id": 1, "status": "done"}, True),
    ({"action": "close", "ticket_id": 1}, True),
    ({"action": "archive"}, True),
    ({"action": "batch_move", "ticket_ids": "1,2", "status": "done"}, True),
    ({"action": "batch_close", "ticket_ids": "1,2"}, True),
    ({"action": "get", "ticket_id": 1}, False),
    ({"action": "list"}, False),
    ({"action": "list_tags"}, False),
]

LINK_CASES = [
    ({"action": "add", "ticket_id": 1, "target_id": 2}, True),
    ({"action": "remove", "link_id": 1}, True),
    ({"action": "list", "ticket_id": 1}, False),
]


def test_every_ticket_action_acquires_with_the_right_gate():
    tools, calls = _tools()
    for kwargs, expected in TICKET_CASES:
        before = len(calls)
        asyncio.run(tools["ticket"](project="p", **kwargs))
        assert len(calls) > before, f"{kwargs['action']} acquired no connection"
        assert calls[-1] is expected, (
            f"ticket action {kwargs['action']!r}: expected require_write="
            f"{expected}, got {calls[-1]}"
        )


def test_every_ticket_link_action_acquires_with_the_right_gate():
    tools, calls = _tools()
    for kwargs, expected in LINK_CASES:
        before = len(calls)
        asyncio.run(tools["ticket_link"](project="p", **kwargs))
        assert len(calls) > before, f"link {kwargs['action']} acquired no connection"
        assert calls[-1] is expected, (
            f"ticket_link action {kwargs['action']!r}: expected require_write="
            f"{expected}, got {calls[-1]}"
        )


def test_board_and_search_are_reads():
    tools, calls = _tools()
    asyncio.run(tools["ticket_board"](project="p"))
    assert calls[-1] is False
    asyncio.run(tools["ticket_search"](query="q", project="p"))
    assert calls[-1] is False
