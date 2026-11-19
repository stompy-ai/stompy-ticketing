"""A card query must not fetch complete append journals to discard them."""

from unittest.mock import MagicMock, patch

from stompy_ticketing.models import TicketListFilters
from stompy_ticketing.service import TicketService
from tests.test_service import _make_ticket_row, _mock_conn_and_cursor


def test_card_query_projects_preview_without_the_complete_body():
    row = _make_ticket_row()
    row.pop("description")
    row.update(description_preview="preview", description_chars=100000)
    conn, cur = _mock_conn_and_cursor()
    cur.fetchall.side_effect = [
        [row],
        [{"status": "backlog", "count": 7}],
        [{"type": "task", "count": 7}],
    ]
    cur.fetchone.return_value = {"count": 7}
    service = TicketService()
    service.archive_stale_tickets = MagicMock(return_value=0)
    with (
        patch("stompy_ticketing.board_view._ticket_columns", return_value=None),
        patch.object(
            service, "to_card", side_effect=AssertionError("full model then discard")
        ),
    ):
        result = service.list_tickets(conn, "fixture", TicketListFilters(limit=1))
    query = str(cur.execute.call_args_list[0].args[0])
    assert "SELECT *" not in query
    assert "description_preview" in query and "description_chars" in query
    assert result.tickets[0].description is None
    assert result.tickets[0].description_preview == "preview"
    assert result.tickets[0].description_chars == 100000


def test_total_is_derived_from_filtered_status_counts():
    conn, cur = _mock_conn_and_cursor()
    cur.fetchall.side_effect = [
        [],
        [{"status": "backlog", "count": 4}, {"status": "done", "count": 3}],
        [{"type": "task", "count": 7}],
    ]
    cur.fetchone.return_value = {"count": 999}
    service = TicketService()
    service.archive_stale_tickets = MagicMock(return_value=0)
    result = service.list_tickets(
        conn, "fixture", TicketListFilters(offset=7), fields="full"
    )
    assert result.total == 7 and not result.has_more
    assert cur.fetchone.call_count == 0
    assert len(cur.execute.call_args_list) == 3
