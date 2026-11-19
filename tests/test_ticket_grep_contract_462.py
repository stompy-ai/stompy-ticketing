"""Fast, database-free checks of the shared predicate and MCP handoff."""

from unittest.mock import MagicMock

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
