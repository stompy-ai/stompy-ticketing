"""Targeted archival must never fall back to the unrelated stale-ticket sweep."""

import json
from unittest.mock import patch

import pytest

from stompy_ticketing.models import TicketResponse
from tests.test_parked_1746 import SCHEMA, _register, _run
from tests.test_service import FIXED_TIME


@pytest.fixture(autouse=True)
def stable_encoding():
    with patch("stompy_ticketing.mcp_tools._toon_encode", side_effect=json.dumps):
        yield


def test_named_archive_targets_that_ticket_without_sweeping():
    ticket, service = _register()
    service.archive_ticket.return_value = TicketResponse(
        id=5,
        title="parked ticket",
        type="task",
        status="parked",
        priority="medium",
        archived_at=FIXED_TIME,
    )
    result = json.loads(_run(ticket(action="archive", ticket_id=5, project=SCHEMA)))
    assert result.get("status") == "archived", result
    assert result["ticket"]["id"] == 5
    assert result["ticket"]["archived_at"] == FIXED_TIME
    service.archive_ticket.assert_called_once()
    assert service.archive_ticket.call_args.args[2] == 5
    service.archive_stale_tickets.assert_not_called()


def test_named_unarchive_is_a_restore_not_a_status_move():
    ticket, service = _register()
    service.unarchive_ticket.return_value = TicketResponse(
        id=5,
        title="closed ticket",
        type="task",
        status="done",
        priority="medium",
    )
    result = json.loads(_run(ticket(action="unarchive", ticket_id=5, project=SCHEMA)))
    assert result.get("status") == "unarchived", result
    assert result["ticket"].get("archived_at") is None
    service.unarchive_ticket.assert_called_once()
    service.transition_ticket.assert_not_called()
    service.archive_stale_tickets.assert_not_called()


def test_no_id_archive_preserves_legacy_sweep_control():
    ticket, service = _register()
    service.archive_stale_tickets.return_value = 3
    result = json.loads(_run(ticket(action="archive", project=SCHEMA)))
    assert result["count"] == 3
    service.archive_stale_tickets.assert_called_once()
    service.archive_ticket.assert_not_called()


def test_batch_ids_on_single_archive_are_still_refused_without_sweep():
    ticket, service = _register()
    result = json.loads(
        _run(ticket(action="archive", ticket_ids="5,6", project=SCHEMA))
    )
    assert result["error"] == "INVALID_PARAMS"
    service.archive_stale_tickets.assert_not_called()
    service.archive_ticket.assert_not_called()
