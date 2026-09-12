"""STOMPY-2145 — an update that updated NOTHING must not report success.

From BUG-213, 2026-09-11:

    ticket(action="update", project="restoboost_team", ticket_id="RT-1035",
           comment="hello")

returns `status: updated` with the ticket record, and stores nothing. The cost,
in the reporter's words: "a full day of tech-lead reviews and a roadblock answer
were written this way ... two builder agents polling the tickets saw nothing and
one filed a roadblock for an answer that had already been posted."

WHERE THE PARAMETER GOES. `ticket()` has no `comment` parameter and no
`**kwargs`, so `comment="hello"` never reaches Python — if it did, it would be
a TypeError. It is dropped by FastMCP's schema validation, which does not
refuse additional properties. These tests CANNOT reproduce that layer: the
suite's `_make_mock_mcp` captures the raw function, so an unknown kwarg raises
here instead of vanishing.

So the fix is deliberately NOT at the schema layer. Whatever a caller sends,
an unknown parameter arrives at this handler as "every recognised field is
None" — and that state is indistinguishable from `update` called with nothing
to change. Refusing THAT covers the reported bug and every future unknown
parameter at once, without depending on how FastMCP is configured.

This is the rule [[1876]] already established elsewhere in the workspace:
a write that wrote nothing is never a success. The file's own
`test_update_with_status_returns_error` applies the same reasoning to `status`
("should return error, not silently ignore") — this extends it to the empty
case rather than inventing a new principle.
"""

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from toon import decode as toon_decode

from stompy_ticketing.mcp_tools import register_ticketing_tools


def _parse(text):
    try:
        return toon_decode(text)
    except Exception:
        return json.loads(text)


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


def _tools(mock_svc):
    """Identical wiring to test_mcp_tools.py's own harness.

    `check_project_func` must return None: a truthy value IS the error response
    the tool returns to the caller, so returning True made every call answer
    `True` and my first draft's controls failed for a reason that had nothing
    to do with the fix.
    """
    mcp = _make_mock_mcp()

    @contextmanager
    def db_ctx(project=None, require_write=True):
        yield MagicMock()

    with patch("stompy_ticketing.mcp_tools.TicketService", return_value=mock_svc):
        register_ticketing_tools(
            mcp_instance=mcp,
            get_db_func=db_ctx,
            check_project_func=MagicMock(return_value=None),
            get_project_func=MagicMock(return_value="test-project"),
        )
    return mcp._registered_tools


class TestAnUpdateThatChangesNothingIsRefused:
    def test_no_updatable_field_returns_an_error(self):
        """The reported bug, reduced: every recognised field absent."""
        svc = MagicMock()
        raw = asyncio.run(_tools(svc)["ticket"](action="update", ticket_id=1))
        data = _parse(raw)

        assert "error" in data, "an update with nothing to update reported success"
        assert data.get("status") != "updated"

    def test_the_database_is_never_touched(self):
        """A refusal that still issues the UPDATE is not a refusal.

        The point is not only the message — it is that we stop pretending a
        write happened. Reading the error while the write still fired would be
        the same defect wearing a warning label.
        """
        svc = MagicMock()
        asyncio.run(_tools(svc)["ticket"](action="update", ticket_id=1))
        svc.update_ticket.assert_not_called()

    def test_the_error_names_what_it_accepts(self):
        """A refusal a caller cannot act on just moves the confusion.

        The agents that hit this were trying to leave commentary. The error has
        to tell them the field names it does take AND point at `append`, which
        is the thing they actually wanted.
        """
        svc = MagicMock()
        raw = asyncio.run(_tools(svc)["ticket"](action="update", ticket_id=1))
        data = _parse(raw)

        # The caller reads the whole payload, not one key, so assert on the
        # whole payload. Pinning this to data["error"] alone would pass or fail
        # on which key the prose happens to live in rather than on whether the
        # caller can act on it.
        rendered = json.dumps(data).lower()

        for field in ("title", "description", "priority", "assignee", "tags"):
            assert field in rendered, f"the refusal should name the updatable field {field!r}"
        assert "append" in rendered, "commentary belongs on append — say so"
        assert "discarded" in rendered, (
            "say plainly that unknown parameters are dropped — that silence is "
            "what cost BUG-213 a day of review notes"
        )


class TestTheControlsThatMustStayGreen:
    """If these go red, the fix refuses too much."""

    def _ok_result(self):
        r = MagicMock()
        r.model_dump.return_value = {"id": 1, "title": "New", "status": "backlog"}
        return r

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"title": "New"},
            {"description": "body"},
            {"priority": "high"},
            {"assignee": "Astra"},
            {"tags": "a,b"},
        ],
        ids=["title", "description", "priority", "assignee", "tags"],
    )
    def test_every_real_single_field_update_still_works(self, kwargs):
        svc = MagicMock()
        svc.update_ticket.return_value = self._ok_result()
        raw = asyncio.run(_tools(svc)["ticket"](action="update", ticket_id=1, **kwargs))
        data = _parse(raw)

        assert "error" not in data, f"{kwargs} is a legitimate update and must not be refused"
        svc.update_ticket.assert_called_once()

    def test_a_missing_ticket_id_still_reports_its_own_error(self):
        """The pre-existing guard must not be shadowed by the new one.

        `ticket_id` missing is a DIFFERENT mistake from "nothing to change",
        and collapsing them would make the more common error less legible.
        """
        svc = MagicMock()
        raw = asyncio.run(_tools(svc)["ticket"](action="update"))
        assert "ticket_id" in _parse(raw)["error"]

    def test_the_status_guard_is_unchanged(self):
        """action=update with status= still tells the caller to use move."""
        svc = MagicMock()
        raw = asyncio.run(
            _tools(svc)["ticket"](action="update", ticket_id=1, status="in_progress")
        )
        err = _parse(raw)["error"].lower()
        assert "move" in err
        svc.update_ticket.assert_not_called()
