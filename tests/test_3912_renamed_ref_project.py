"""STOMPY-3912: a prefixed ref and the project's CURRENT name are the same project.

The host resolves a ticket prefix to the project's schema. After a rename the
caller's project name differs from that schema, and the old string comparison
refused every prefixed ref with PROJECT_MISMATCH (29 times in 5 days on prod).
"""

import asyncio
from contextlib import contextmanager
from unittest.mock import MagicMock

from stompy_ticketing.mcp_tools import register_ticketing_tools
from tests.tool_response import parse_tool_response

SCHEMAS = {"kuiske_hallinto": "tuike_hallinto", "tuike_hallinto": "tuike_hallinto",
           "other": "other"}


def _register():
    registered, seen = {}, {"db_projects": []}
    mcp = MagicMock()
    mcp.tool = lambda: (lambda f: registered.setdefault(f.__name__, f))

    @contextmanager
    def db_ctx(project=None, require_write=True):
        seen["db_projects"].append(project)
        conn, cursor = MagicMock(), MagicMock()
        cursor.fetchone.return_value = None
        cursor.fetchall.return_value = []
        conn.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        yield conn

    def resolve_schema(name):
        if name not in SCHEMAS:
            raise ValueError(f"unknown project {name}")
        return SCHEMAS[name]

    register_ticketing_tools(
        mcp_instance=mcp,
        get_db_func=db_ctx,
        check_project_func=MagicMock(return_value=None),
        get_project_func=MagicMock(side_effect=lambda p=None: p or "default"),
        resolve_schema_func=resolve_schema,
        resolve_prefix_func=lambda prefix: {"TH": "tuike_hallinto"}.get(prefix),
        get_prefix_func=lambda project: "TH",
    )
    return registered, seen


def _call(**kwargs):
    tools, seen = _register()
    return parse_tool_response(asyncio.run(tools["ticket"](**kwargs))), seen


def test_ref_with_the_current_name_of_its_renamed_project_is_accepted():
    payload, seen = _call(action="get", ticket_id="TH-12", project="kuiske_hallinto")
    assert payload.get("error") != "PROJECT_MISMATCH", payload
    assert seen["db_projects"] == ["kuiske_hallinto"]


def test_batch_refs_with_the_current_name_are_accepted():
    payload, seen = _call(action="batch_close", ticket_ids="TH-1,TH-2",
                          project="kuiske_hallinto")
    assert payload.get("error") != "PROJECT_MISMATCH", payload


def test_a_really_different_project_is_still_refused():
    payload, seen = _call(action="get", ticket_id="TH-12", project="other")
    assert payload.get("error") == "PROJECT_MISMATCH", payload
    assert seen["db_projects"] == []


def test_an_unresolvable_project_is_still_refused():
    payload, seen = _call(action="get", ticket_id="TH-12", project="nope")
    assert payload.get("error") == "PROJECT_MISMATCH", payload
    assert seen["db_projects"] == []


def test_link_between_refs_of_the_renamed_project_is_not_a_cross_project_link():
    tools, _ = _register()
    out = parse_tool_response(asyncio.run(tools["ticket_link"](
        action="add", ticket_id="TH-1", target_id="TH-2", project="kuiske_hallinto")))
    assert out.get("error") != "PROJECT_MISMATCH", out
