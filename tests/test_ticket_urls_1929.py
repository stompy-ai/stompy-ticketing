"""STOMPY-1929 — a ticket carries the address a human can click.

Both the URL GRAMMAR and the SHAPE RULE (a per-object `url`, but ONE
`url_template` on a list of cards — STOMPY-1925) live in the host
(src/services/object_urls.stamp_urls) and are injected as ``stamp_urls_func``
at registration, so the MCP door and the REST door cannot drift (STOMPY-1927).
This module owns only the plumbing: bind the project for the call, hand the
payload over, and accept a full URL wherever a ticket reference is accepted.

RED without the 1929 commit: `_bind_display` and `stamp_urls_func` do not
exist, and `coerce_ticket_ref` raises TicketRefError on a URL.
"""

from pathlib import Path

import pytest

from stompy_ticketing import mcp_tools
from stompy_ticketing.refs import TicketRefError, coerce_ticket_ref

BASE = "https://www.stompy.ai"


def _url(project, ref):
    return f"{BASE}/dashboard/projects/{project}/tickets/{ref}"


def _fake_stamp(payload, project):
    """Stand-in for the host's stamp_urls: object -> url, list -> template.
    The REAL rule is tested in the host repo; this pins the WIRING."""
    if isinstance(payload, dict):
        rows = payload.get("tickets")
        if isinstance(rows, list) and rows:
            payload["url_template"] = f"{BASE}/dashboard/projects/{project}/tickets/{{display_id}}"
        elif isinstance(payload.get("id"), int) and "title" in payload:
            payload["url"] = _url(project, payload.get("display_id") or payload["id"])
    return payload


@pytest.fixture
def bound(monkeypatch):
    """Bind a call the way each tool does, with the host stamper injected."""
    monkeypatch.setattr(mcp_tools, "_stamp_urls_func", _fake_stamp)
    token = mcp_tools._bind_display("stompy", "STOMPY")
    yield
    mcp_tools._unbind_display(token)


class TestPayloadsGoThroughTheHostStamper:
    def test_a_single_ticket_is_addressed(self, bound):
        out = mcp_tools._safe_json({"id": 1929, "title": "t", "status": "in_progress"})
        assert _url("stompy", "STOMPY-1929") in out

    def test_display_id_is_stamped_before_the_url_is_built(self, bound):
        """Ordering matters: the address is built FROM display_id."""
        row = {"id": 1929, "title": "t", "status": "open"}
        out = mcp_tools._safe_json(row)
        assert "STOMPY-1929" in out and "/tickets/STOMPY-1929" in out

    def test_a_card_list_gets_the_template_the_host_chose(self, bound):
        payload = {"tickets": [{"id": i, "title": "a", "status": "open"} for i in (1, 2)], "total": 2}
        out = mcp_tools._safe_json(payload)
        assert "/tickets/{display_id}" in out

    def test_no_stamper_no_addresses(self, monkeypatch):
        """A host that predates 1929 injects nothing — payloads are unchanged."""
        monkeypatch.setattr(mcp_tools, "_stamp_urls_func", None)
        token = mcp_tools._bind_display("stompy", "STOMPY")
        try:
            out = mcp_tools._safe_json({"id": 1, "title": "t", "status": "open"})
            assert "dashboard/projects" not in out
            assert "STOMPY-1" in out
        finally:
            mcp_tools._unbind_display(token)

    def test_unbound_calls_emit_nothing(self, monkeypatch):
        monkeypatch.setattr(mcp_tools, "_stamp_urls_func", _fake_stamp)
        out = mcp_tools._safe_json({"id": 1, "title": "t", "status": "open"})
        assert "dashboard/projects" not in out

    def test_stamper_failure_never_breaks_a_response(self, monkeypatch, bound):
        monkeypatch.setattr(
            mcp_tools, "_stamp_urls_func", lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
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


class TestTheDescriptionSaysSo:
    """STOMPY-1924 description truth: a description that omits a capability is
    the dogfood-20260727 contract-drift class. If ticket(get) takes a URL, the
    schema an agent reads has to say so."""

    def _ticket_id_annotation(self):
        import re

        src = Path(mcp_tools.__file__).read_text()
        block = src[src.index("        ticket_id: Annotated["):]
        return re.split(r"\n        ticket_ids:", block)[0]

    def test_ticket_id_description_names_the_url_form(self):
        text = self._ticket_id_annotation().lower()
        assert "url" in text, "ticket(get) accepts a URL but its description does not say so"
