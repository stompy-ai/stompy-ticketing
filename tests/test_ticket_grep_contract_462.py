"""Fast, database-free checks of the shared predicate and MCP handoff."""

import fnmatch
import re
from unittest.mock import MagicMock, patch

import pytest
from psycopg2.errors import InvalidRegularExpression

from stompy_ticketing.models import TicketListFilters, TicketListResponse
from stompy_ticketing.service import TicketService
from stompy_ticketing.ticket_listing import glob_regex
from tests.test_cluster_c_fixes import _register, _run
from tests.test_service import _mock_conn_and_cursor


def test_all_list_queries_bind_the_same_title_predicate():
    conn, cur = _mock_conn_and_cursor()
    cur.fetchall.side_effect = [[], [], []]
    cur.fetchone.return_value = {"count": 0}
    service = TicketService()
    service.archive_stale_tickets = MagicMock(return_value=0)
    pattern = "a'b%_[]*"
    service.list_tickets(conn, "fixture", TicketListFilters(grep=pattern))
    calls = cur.execute.call_args_list
    assert len(calls) == 4
    for query, params in (call.args for call in calls):
        assert 'title COLLATE "C" ~ %s' in str(query)
        assert pattern not in str(query)
        assert params[0] == glob_regex(pattern)


def test_mcp_forwards_glob_and_retains_out_of_page_matches():
    service = MagicMock()
    service.list_tickets.return_value = TicketListResponse(
        tickets=[],
        total=3,
        limit=1,
        offset=3,
        has_more=False,
        by_status={"backlog": 3},
        by_type={"task": 3},
    )
    tools, _ = _register(mock_svc=service)
    from toon import decode

    result = decode(
        _run(tools["ticket"](action="list", grep="auth*", limit=1, offset=3))
    )
    assert service.list_tickets.call_args.args[2].grep == "auth*"
    assert result["total"] == 3 and result.get("tickets", []) == []
    assert result["by_status"] == {"backlog": 3}


def test_default_filter_leaves_glob_unset():
    assert TicketListFilters().grep is None


def test_translation_is_independent_of_cpython_regex_generation():
    with patch.object(
        fnmatch, "translate", side_effect=AssertionError("CPython internals")
    ):
        translated = glob_regex("*a*b*")
    assert translated == r"(?s)\A.*a.*b.*\Z"


def test_multistar_translation_has_no_python_only_group_constructs():
    translated = glob_regex("*a*b*")
    assert all(token not in translated for token in ("(?P", "(?=", "(?>"))


@pytest.mark.parametrize(
    "pattern",
    [
        "*a*b*",
        "[a-c]",
        "[!a-c]",
        "[c-a]",
        "[!c-a]",
        "[-a]",
        "[a-]",
        "[]a]",
        "[!]]",
        "[[]",
        "[^a]",
        "[a--b]",
        "[a-b-c]",
        "[a&&b]",
        "[\\]",
        "[",
        "[!]",
    ],
)
def test_fast_glob_language_matches_oracle(pattern):
    names = ["", "a", "b", "c", "-", "]", "[", "^", "&", "\\", "\n", "aab", "café"]
    expected = [name for name in names if fnmatch.fnmatch(name, pattern)]
    actual = [name for name in names if re.match(glob_regex(pattern), name)]
    assert actual == expected


def test_postgres_pattern_rejection_is_a_client_refusal():
    conn, cur = _mock_conn_and_cursor()
    cur.execute.side_effect = InvalidRegularExpression("private database diagnostic")
    service = TicketService()
    service.archive_stale_tickets = MagicMock(return_value=0)
    with pytest.raises(ValueError, match="Title glob") as refusal:
        service.list_tickets(conn, "fixture", TicketListFilters(grep="a*"))
    assert "private database diagnostic" not in str(refusal.value)
    conn.rollback.assert_called_once()


def test_mcp_pattern_rejection_is_not_an_internal_error():
    import json

    service = MagicMock()
    service.list_tickets.side_effect = ValueError("Title glob could not be matched")
    tools, _ = _register(mock_svc=service)
    result = json.loads(_run(tools["ticket"](action="list", grep="a*")))
    assert result["error_type"] == "ValueError"
    assert result["error"].startswith("Title glob")
