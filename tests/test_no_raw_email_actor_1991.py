"""An ADDRESS never rides the raw actor field (STOMPY-1991).

`created_by` and `changed_by` hold an identity (STOMPY-1594). Rows written
before the host stopped stamping emails hold a literal address, and a ticket
payload is readable by every member of a shared project — so filling only the
`*_display` fields left the filer's address on the wire on both doors. The
host's resolver omits ids it cannot name, which made the leak invisible to a
test that only looked at the display field.

The gate here is the one the dogfood uses: grep the WHOLE payload for `@`.
"""

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from stompy_ticketing import mcp_tools
from stompy_ticketing.actors import ADDRESS_PLACEHOLDER, redact_actors, safe_actor
from stompy_ticketing.models import TicketHistoryEntry, TicketResponse

LEGACY = "jeroen@example.com"
MINE = "markus@stompy.ai"


def _addresses(payload) -> list:
    text = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    return [w for w in text.replace('"', " ").replace(",", " ").split() if "@" in w]


def _ticket(created_by=LEGACY, history_actor=LEGACY):
    return TicketResponse(
        id=7,
        title="a bug report",
        type="bug",
        status="triage",
        priority="medium",
        created_by=created_by,
        history=[
            TicketHistoryEntry(id=1, field_name="status", changed_by=history_actor, changed_at=1.0)
        ],
    )


@contextmanager
def _db(project=None, require_write=True):
    yield MagicMock()


def _register(display=None):
    """The same harness the 1594 suite uses — one registration shape, so a
    signature change breaks both files rather than silently skipping this
    one."""
    tools = {}
    mcp = MagicMock()
    mcp.tool.return_value = lambda fn: tools.__setitem__(fn.__name__, fn) or fn
    service = MagicMock()
    with patch.object(mcp_tools, "TicketService", return_value=service):
        mcp_tools.register_ticketing_tools(
            mcp_instance=mcp,
            get_db_func=_db,
            check_project_func=MagicMock(return_value=None),
            get_project_func=MagicMock(side_effect=lambda p=None: p or "proj"),
            actor_func=lambda: "51",
            display_actors_func=display,
        )
    return tools["ticket"], service


# --------------------------------------------------------------------- the rule
class TestSafeActor:
    def test_a_numeric_id_passes_through(self):
        assert safe_actor("5692", {}) == "5692"

    def test_none_passes_through(self):
        assert safe_actor(None, {}) is None

    def test_an_address_becomes_the_hosts_answer(self):
        assert safe_actor(LEGACY, {LEGACY: "brave-fox-7"}) == "brave-fox-7"

    def test_an_address_the_host_will_not_name_becomes_the_placeholder(self):
        assert safe_actor(LEGACY, {}) == ADDRESS_PLACEHOLDER
        assert "@" not in safe_actor(LEGACY, {})

    def test_the_placeholder_is_not_an_id_anyone_could_mistake_for_one(self):
        assert not ADDRESS_PLACEHOLDER.isdigit()

    def test_redact_tolerates_a_ticket_with_no_actors(self):
        bare = TicketResponse(id=1, title="t", type="task", status="backlog", priority="none")
        assert redact_actors(bare, {}) is bare


# ------------------------------------------------------------------ MCP door
class TestMcpDoor:
    def test_a_legacy_email_actor_never_reaches_the_payload(self):
        tool, service = _register(display=lambda ids: {})
        service.get_ticket.return_value = _ticket()
        out = asyncio.run(tool(action="get", ticket_id=7, project="proj"))
        assert _addresses(out) == [], out
        assert ADDRESS_PLACEHOLDER in out

    def test_the_hosts_name_is_used_when_it_has_one(self):
        tool, service = _register(display=lambda ids: {LEGACY: "brave-fox-7"})
        service.get_ticket.return_value = _ticket()
        out = asyncio.run(tool(action="get", ticket_id=7, project="proj"))
        assert _addresses(out) == []
        assert "brave-fox-7" in out

    def test_the_readers_own_address_may_come_back(self):
        """The host decides that — `attribution_display` returns the viewer's
        OWN address as a last resort, and this module only refuses to print
        what the host withheld."""
        tool, service = _register(display=lambda ids: {MINE: MINE})
        service.get_ticket.return_value = _ticket(created_by=MINE, history_actor=MINE)
        out = asyncio.run(tool(action="get", ticket_id=7, project="proj"))
        # Four: created_by + its display, and the history row's pair. Every
        # one of them is the READER'S OWN address, which is the only address
        # the rule allows.
        assert set(_addresses(out)) == {MINE} and len(_addresses(out)) == 4

    def test_a_numeric_actor_is_untouched(self):
        tool, service = _register(display=lambda ids: {"51": "Markus"})
        service.get_ticket.return_value = _ticket(created_by="51", history_actor="51")
        out = asyncio.run(tool(action="get", ticket_id=7, project="proj"))
        assert "created_by: 51" in out or 'created_by: "51"' in out
        assert "created_by_display: Markus" in out

    def test_a_raising_resolver_still_redacts(self):
        """The degradation path is where a leak hides: when the host cannot be
        reached the display is dropped, and the raw field is all that is
        left."""

        def boom(ids):
            raise RuntimeError("MAIN unreachable")

        tool, service = _register(display=boom)
        service.get_ticket.return_value = _ticket()
        out = asyncio.run(tool(action="get", ticket_id=7, project="proj"))
        assert _addresses(out) == [], out

    def test_no_resolver_at_all_still_redacts(self):
        tool, service = _register(display=None)
        service.get_ticket.return_value = _ticket()
        out = asyncio.run(tool(action="get", ticket_id=7, project="proj"))
        assert _addresses(out) == [], out


# ----------------------------------------------------------------- REST door
class TestRestDoor:
    """Two doors, one tested is not tested. This door resolves no display
    names (that is STOMPY-1454's work), so the placeholder is all it can
    honestly print — but it must never print the address."""

    def _get(self, ticket):
        from stompy_ticketing import api_routes

        conn = MagicMock()
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=conn)
        ctx.__exit__ = MagicMock(return_value=False)
        with patch.object(api_routes, "_get_db_for_project", lambda *a, **k: ctx), patch.object(
            api_routes, "_resolve_schema", lambda n: n
        ), patch.object(api_routes._service, "get_ticket", return_value=ticket):
            return asyncio.run(api_routes.get_ticket("proj", 7))

    def test_a_legacy_email_actor_never_reaches_the_wire(self):
        out = self._get(_ticket())
        assert _addresses(out.model_dump()) == []
        assert out.created_by == ADDRESS_PLACEHOLDER
        assert out.history[0].changed_by == ADDRESS_PLACEHOLDER

    def test_a_numeric_actor_is_untouched(self):
        out = self._get(_ticket(created_by="51", history_actor="51"))
        assert out.created_by == "51"
        assert out.history[0].changed_by == "51"

    def test_a_missing_ticket_still_404s(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as caught:
            self._get(None)
        assert caught.value.status_code == 404
