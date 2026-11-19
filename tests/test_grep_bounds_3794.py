"""Request bounds and transactional timeout for the existing ticket-list path."""

from unittest.mock import MagicMock

import pytest
from psycopg2.errors import QueryCanceled
from pydantic import ValidationError

from stompy_ticketing.models import TicketListFilters, TicketListResponse
from stompy_ticketing.service import TicketService
from tests.test_cluster_c_fixes import _register, _run
from tests.tool_response import parse_tool_response


def test_10000_character_glob_refuses_before_database():
    service = MagicMock()
    service.list_tickets.return_value = TicketListResponse(
        tickets=[],
        total=0,
        limit=20,
        offset=0,
        has_more=False,
        by_status={},
        by_type={},
    )
    tools, calls = _register(mock_svc=service)
    result = parse_tool_response(_run(tools["ticket"](action="list", grep="x" * 10000)))
    assert result.get("error") == "INVALID_ARGUMENT"
    assert "200" in result["message"] and "x" * 100 not in result["message"]
    assert calls["n"] == 0
    service.list_tickets.assert_not_called()


def test_shared_filter_refuses_over_200_characters():
    with pytest.raises(ValidationError):
        TicketListFilters(grep="é" * 201)
    assert TicketListFilters(grep="é" * 200).grep == "é" * 200


def test_normal_glob_control_is_forwarded_unchanged():
    service = MagicMock()
    service.list_tickets.return_value = TicketListResponse(
        tickets=[],
        total=0,
        limit=20,
        offset=0,
        has_more=False,
        by_status={},
        by_type={},
    )
    tools, calls = _register(mock_svc=service)
    answer = parse_tool_response(_run(tools["ticket"](action="list", grep="auth*")))
    assert answer["total"] == 0 and not answer["has_more"]
    assert service.list_tickets.call_args.args[2].grep == "auth*"
    assert calls["n"] == 1


def test_cancelled_list_rolls_back_and_refuses_as_value_error():
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.execute.side_effect = QueryCanceled(
        "statement timeout"
    )
    service = TicketService()
    service.archive_stale_tickets = MagicMock(return_value=0)
    with pytest.raises(ValueError, match="(?i)time.*narrow|narrow.*time"):
        service.list_tickets(conn, "fixture", TicketListFilters(grep="auth*"))
    conn.rollback.assert_called_once()
