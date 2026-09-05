"""STOMPY-1929 — a ticket carries the address a human can click.

The URL GRAMMAR itself lives in the host (src/services/object_urls.py) and is
injected as ``ticket_url_func`` at registration, so the MCP door and the REST
door build the same string from one implementation (STOMPY-1927). This module
owns only the plumbing: bind the project for the call, stamp the rows, and
accept a full URL wherever a ticket reference is accepted.

RED without the 1929 commit: `_bind_display` and `ticket_url_func` do not
exist, and `coerce_ticket_ref` raises TicketRefError on a URL.
"""

import pytest

from stompy_ticketing import mcp_tools
from stompy_ticketing.refs import TicketRefError, coerce_ticket_ref

BASE = "https://www.stompy.ai"


def _url(project, ref):
    return f"{BASE}/dashboard/projects/{project}/tickets/{ref}"


@pytest.fixture
def bound(monkeypatch):
    """Bind a call the way each tool does, with a host-injected builder."""
    monkeypatch.setattr(mcp_tools, "_ticket_url_func", _url)
    token = mcp_tools._bind_display("stompy", "STOMPY")
    yield
    mcp_tools._unbind_display(token)


class TestUrlOnMcpTicketPayloads:
    def test_ticket_row_gains_a_url(self, bound):
        out = mcp_tools._safe_json({"id": 1929, "title": "t", "status": "in_progress"})
        assert _url("stompy", "STOMPY-1929") in out

    def test_url_uses_the_display_id(self, bound):
        row = {"id": 1929, "title": "t", "status": "open"}
        mcp_tools._decorate_display_ids(row, "STOMPY", "stompy")
        assert row["display_id"] == "STOMPY-1929"
        assert row["url"].endswith("/tickets/STOMPY-1929")

    def test_nested_board_columns(self, bound):
        payload = {"columns": [{"status": "open", "tickets": [{"id": 1, "title": "a", "status": "open"}]}]}
        mcp_tools._decorate_display_ids(payload, "STOMPY", "stompy")
        assert payload["columns"][0]["tickets"][0]["url"].endswith("/tickets/STOMPY-1")

    def test_no_builder_no_url(self, monkeypatch):
        """A host that predates 1929 injects nothing — the payload is unchanged."""
        monkeypatch.setattr(mcp_tools, "_ticket_url_func", None)
        token = mcp_tools._bind_display("stompy", "STOMPY")
        try:
            row = {"id": 1, "title": "t", "status": "open"}
            mcp_tools._decorate_display_ids(row, "STOMPY", "stompy")
            assert "url" not in row
            assert row["display_id"] == "STOMPY-1"
        finally:
            mcp_tools._unbind_display(token)

    def test_unbound_calls_emit_nothing(self, monkeypatch):
        monkeypatch.setattr(mcp_tools, "_ticket_url_func", _url)
        out = mcp_tools._safe_json({"id": 1, "title": "t", "status": "open"})
        assert "dashboard/projects" not in out

    def test_existing_url_is_not_overwritten(self, bound):
        row = {"id": 1, "title": "t", "status": "open", "url": "https://elsewhere/x"}
        mcp_tools._decorate_display_ids(row, "STOMPY", "stompy")
        assert row["url"] == "https://elsewhere/x"

    def test_builder_failure_never_breaks_a_response(self, monkeypatch, bound):
        monkeypatch.setattr(
            mcp_tools, "_ticket_url_func", lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        )
        out = mcp_tools._safe_json({"id": 1, "title": "t", "status": "open"})
        assert "STOMPY-1" in out


class TestTicketRefAcceptsUrls:
    """Rule 3: ticket(get) takes the link a human was sent."""

    def test_url_resolves_to_project_and_id(self):
        assert coerce_ticket_ref(_url("stompy", "STOMPY-1929"), "other", lambda p: "stompy") == (
            "stompy",
            1929,
        )

    def test_numeric_url_keeps_its_own_project(self):
        assert coerce_ticket_ref(_url("myproj", "42"), "other") == ("myproj", 42)

    def test_staging_url(self):
        assert coerce_ticket_ref(
            "https://staging.stompy.ai/dashboard/projects/p/tickets/7", "other"
        ) == ("p", 7)

    def test_context_url_is_refused_with_a_useful_message(self):
        with pytest.raises(TicketRefError) as exc:
            coerce_ticket_ref(f"{BASE}/dashboard/projects/p/contexts/topic", "p")
        assert "context" in str(exc.value).lower()

    def test_bare_forms_still_work(self):
        assert coerce_ticket_ref(1311, "p") == ("p", 1311)
        assert coerce_ticket_ref("1311", "p") == ("p", 1311)
        assert coerce_ticket_ref("STOMPY-1311", "p", lambda x: "stompy") == ("stompy", 1311)

    def test_a_foreign_url_is_still_an_unrecognised_ref(self):
        with pytest.raises(TicketRefError):
            coerce_ticket_ref("https://example.com/dashboard/projects/p/tickets/1", "p")
