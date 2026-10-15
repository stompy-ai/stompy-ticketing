"""STOMPY-2839: target_title must be the title of the row's target_id ticket,
whichever side of the link the viewed ticket is on."""

from unittest.mock import MagicMock

from stompy_ticketing.service import TicketService
from tests.test_service import _sql_to_str

SCHEMA = "user_1"


def _executed_sql(method, *args):
    cur = MagicMock()
    cur.fetchall.return_value = []
    getattr(TicketService(), method)(cur, SCHEMA, *args)
    return [_sql_to_str(c.args[0]) for c in cur.execute.call_args_list]


def test_get_links_for_ticket_titles_come_from_target_side_2839():
    for q in _executed_sql("_get_links_for_ticket", 2):
        assert "t.id = tl.source_id" not in q
        assert "t.id = tl.target_id" in q


def test_bulk_links_titles_come_from_target_side_2839():
    for q in _executed_sql("_get_links_for_tickets_bulk", [1, 2]):
        assert "t.id = tl.source_id" not in q
        assert "t.id = tl.target_id" in q


def test_bulk_links_attributed_to_both_endpoints_2839():
    cur = MagicMock()
    cur.fetchall.return_value = [
        {"id": 1, "source_id": 1, "target_id": 2, "link_type": "blocks",
         "created_at": 0.0, "target_title": "two", "target_status": "backlog"}
    ]
    out = TicketService()._get_links_for_tickets_bulk(cur, SCHEMA, [1, 2])
    assert [l.target_title for l in out[1]] == ["two"]
    assert [l.target_title for l in out[2]] == ["two"]
