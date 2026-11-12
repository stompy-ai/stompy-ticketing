"""STOMPY-3551: the assignee filter existed only on MCP's ticket list. The REST
ticket list is the board view, which could not filter by assignee or priority,
so `?assignee=` was silently ignored and every ticket came back. board_view now
filters both in SQL; the assignee matches as list_tickets does (3544: trimmed,
case-insensitive), so every door agrees on who a ticket belongs to."""

from unittest.mock import MagicMock

from stompy_ticketing.service import TicketService
from tests.board_fake import board_conn

SCHEMA = "test_project"


def _board(**kw):
    service = TicketService()
    service.archive_stale_tickets = MagicMock(return_value=0)
    conn, cur = board_conn([])
    service.board_view(conn, SCHEMA, **kw)
    return cur


def test_assignee_filters_trimmed_and_case_insensitive_3551():
    cur = _board(assignee=" ASTRA ")
    for text, params in cur.board_queries():
        if "archived_at IS NOT NULL" in text:
            continue
        assert "lower(btrim(assignee" in text, text
        assert " ASTRA " in params, params


def test_priority_filters_every_board_query_3551():
    cur = _board(priority="high")
    for text, params in cur.board_queries():
        if "archived_at IS NOT NULL" in text:
            continue
        assert "priority = %s" in text, text
        assert "high" in params, params


def test_no_filter_adds_no_condition_3551():
    cur = _board()
    for text, _ in cur.board_queries():
        assert "btrim(assignee" not in text and "priority = %s" not in text
