"""Ticket reference coercion — the contract every tool param goes through.

Pins the review findings from the 2026-07-27 design pass:
- #8: digit-strings ("1311") must take the int path, never the prefix parser
- case-insensitive prefixed input, canonical UPPER
- unknown prefix / no resolver / garbage → TicketRefError with guidance,
  never a silent mis-parse
"""

import pytest

from stompy_ticketing.refs import TicketRefError, coerce_ticket_ref, format_display_id

PROJ = "test_project"


def _resolver(prefix):
    return {"STOMPY": "stompy", "BUG": "bug_inbox"}.get(prefix)


class TestCoercion:
    def test_int_uses_current_project(self):
        assert coerce_ticket_ref(1311, PROJ, _resolver) == (PROJ, 1311)

    def test_digit_string_takes_int_path_not_prefix_parser(self):
        assert coerce_ticket_ref("1311", PROJ, _resolver) == (PROJ, 1311)

    def test_prefixed_ref_resolves_project(self):
        assert coerce_ticket_ref("STOMPY-42", PROJ, _resolver) == ("stompy", 42)

    def test_prefixed_ref_is_case_insensitive(self):
        assert coerce_ticket_ref("bug-188", PROJ, _resolver) == ("bug_inbox", 188)

    def test_none_returns_none_id(self):
        assert coerce_ticket_ref(None, PROJ, _resolver) == (PROJ, None)

    def test_unknown_prefix_raises_with_guidance(self):
        with pytest.raises(TicketRefError, match="Unknown ticket prefix"):
            coerce_ticket_ref("NOPE-1", PROJ, _resolver)

    def test_no_resolver_raises_not_misparses(self):
        with pytest.raises(TicketRefError, match="not supported by this host"):
            coerce_ticket_ref("STOMPY-42", PROJ, None)

    @pytest.mark.parametrize("bad", ["BUG-", "BUG", "-42", "B UG-1", "BUG-1x", "9BUG-1", ""])
    def test_garbage_raises(self, bad):
        with pytest.raises(TicketRefError):
            coerce_ticket_ref(bad, PROJ, _resolver)

    def test_whitespace_tolerated_around_ref(self):
        assert coerce_ticket_ref("  STOMPY-7 ", PROJ, _resolver) == ("stompy", 7)


class TestDisplayId:
    def test_with_prefix(self):
        assert format_display_id("STOMPY", 1311) == "STOMPY-1311"

    def test_without_prefix_falls_back_to_plain(self):
        assert format_display_id(None, 7) == "7"
