"""A database exception the four tools catch is answered in OUR words (STOMPY-3089 follow-up).

Before: every tool's catch-all answered ``"<Tool> failed: {str(e)[:200]}"`` — the
driver's sentence (a relation name, "canceling statement due to statement timeout",
a host) reached the caller. One test per sentence of the fix:

  1. no tool's answer carries the exception's text; it carries a stable code and a
     reference id instead;
  2. the real error is logged, with the same reference;
  3. a statement cancel is the retryable TOOL_DB_TIMEOUT (never a connection code);
  4. a failed connection is the retryable CONNECTION_TIMEOUT, a dropped one
     CONNECTION_DROPPED;
  5. the host's typed access refusals and our own ValueError refusals keep their
     client-class answer (the host books those by ``error_type``).
"""

import asyncio
import json
import logging
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import psycopg2
import psycopg2.errors
import pytest

from stompy_ticketing.mcp_tools import register_ticketing_tools

_SECRET = "SELECT secret FROM s1.t host s1.db"
_FAILURES = {
    "cancel": lambda: psycopg2.errors.QueryCanceled("canceling statement due to statement timeout"),
    "connect": lambda: psycopg2.OperationalError("could not connect to server: host s1.db"),
    "closed": lambda: psycopg2.InterfaceError("connection already closed"),
    "generic": lambda: Exception(_SECRET),
}
_DRIVER_TEXT = (
    "canceling statement",
    "could not connect",
    "connection already closed",
    "SELECT secret",
    "s1.t",
    "s1.db",
    "failed: ",
    "QueryCanceled",
    "OperationalError",
    "InterfaceError",
)
_CALLS = {
    "ticket": dict(action="get", ticket_id=1, project="p"),
    "ticket_link": dict(action="list", ticket_id=1, project="p"),
    "ticket_board": dict(project="p"),
    "ticket_search": dict(query="x", project="p"),
}
_SITE_CODE = {
    "ticket": "TICKET_FAILED",
    "ticket_link": "LINK_FAILED",
    "ticket_board": "BOARD_UNAVAILABLE",
    "ticket_search": "SEARCH_FAILED",
}


def _tools(make_exc):
    registered = {}
    mcp = MagicMock()

    def tool():
        def deco(fn):
            registered[fn.__name__] = fn
            return fn

        return deco

    mcp.tool = tool

    @contextmanager
    def db_ctx(project=None, require_write=True):
        raise make_exc()
        yield  # pragma: no cover

    with patch("stompy_ticketing.mcp_tools.TicketService"):
        register_ticketing_tools(
            mcp_instance=mcp,
            get_db_func=db_ctx,
            check_project_func=MagicMock(return_value=None),
            get_project_func=MagicMock(return_value="p"),
        )
    return registered


def _call(name, make_exc):
    return json.loads(asyncio.run(_tools(make_exc)[name](**_CALLS[name])))


_TOOLS = sorted(_CALLS)


@pytest.mark.parametrize("kind", sorted(_FAILURES))
@pytest.mark.parametrize("name", _TOOLS)
def test_no_answer_carries_the_exceptions_text(name, kind):
    out = _call(name, _FAILURES[kind])
    text = json.dumps(out)
    assert not [m for m in _DRIVER_TEXT if m in text], text
    assert out["success"] is False and out["error"].isupper() and "error_type" not in out
    assert out["recovery"]["can_retry"] is True


@pytest.mark.parametrize("name", _TOOLS)
def test_anything_else_is_the_sites_code_with_a_reference_the_log_carries(name, caplog):
    with caplog.at_level(logging.ERROR, logger="stompy_ticketing.db_error_answer"):
        out = _call(name, _FAILURES["generic"])
    assert out["error"] == _SITE_CODE[name]
    assert out["reference"] and out["reference"] in out["message"]
    (rec,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert rec.reference == out["reference"] and rec.tool == name
    # The host formats stdlib records as "%(message)s": `extra` fields never reach its
    # log, so the reference, tool and class must be IN the message to be findable.
    message = rec.getMessage()
    assert f"reference={out['reference']}" in message and f"tool={name}" in message
    assert _SECRET in rec.getMessage() or _SECRET in str(rec.exc_info[1])


@pytest.mark.parametrize("name", _TOOLS)
def test_a_statement_cancel_is_the_retryable_tool_db_timeout(name):
    out = _call(name, _FAILURES["cancel"])
    assert out["error"] == "TOOL_DB_TIMEOUT" and name in out["message"]
    assert "connection" not in out["message"].lower()


@pytest.mark.parametrize("name", _TOOLS)
@pytest.mark.parametrize("kind,code", [("connect", "CONNECTION_TIMEOUT"), ("closed", "CONNECTION_DROPPED")])
def test_a_connection_failure_is_a_retryable_connection_code(name, kind, code):
    out = _call(name, _FAILURES[kind])
    assert out["error"] == code and out["reference"]


def test_board_does_not_retry_a_statement_cancel():
    """A cancel is not an idle-connection drop: retrying it only doubles the wait."""
    attempts = {"n": 0}

    def cancel():
        attempts["n"] += 1
        return _FAILURES["cancel"]()

    _call("ticket_board", cancel)
    assert attempts["n"] == 1


class ProjectWriteDenied(Exception):
    """Stands in for the host's typed access refusal (matched by class name)."""


@pytest.mark.parametrize("name", _TOOLS)
def test_host_access_refusals_keep_their_client_error_type(name):
    out = _call(name, lambda: ProjectWriteDenied("You have read-only access to project 'p'"))
    assert out["error_type"] == "ProjectWriteDenied"  # unchanged: a guard, green before and after
    assert "You have read-only access to project 'p'" in out["error"]


@pytest.mark.parametrize("name", ["ticket_board", "ticket_search"])
def test_our_value_errors_stay_client_refusals(name):
    out = _call(name, lambda: ValueError("limit must be positive"))
    assert out["error_type"] == "ValueError"  # unchanged: a guard, green before and after
    assert "limit must be positive" in out["error"]
