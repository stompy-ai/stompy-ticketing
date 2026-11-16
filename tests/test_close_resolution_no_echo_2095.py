"""STOMPY-2095: a refused close never echoes the caller's `resolution` text.

`resolution` names the terminal STATUS. Agents keep passing their closing note
there; the refusal used to quote that note in full ("'<note>' is not a terminal
status"), so a customer's prose landed verbatim in the host's error_events rows
(prod, 2026-11-13..15, six rows for one beta user). The refusal must say which
values are valid and how to record a note, and must not contain the input.
"""

from unittest.mock import MagicMock

import pytest

from stompy_ticketing.service import InvalidTransitionError, TicketService

SCHEMA = "test_project"
NOTE = "Toteutettu ja testattu: PR #2 merged, verified on device, customer-private detail"


def _conn(*fetchone):
    conn, cur = MagicMock(), MagicMock()
    conn.cursor.return_value = cur
    cur.fetchone.side_effect = list(fetchone)
    cur.fetchall.return_value = []
    return conn


def test_close_refusal_names_valid_terminals_without_echoing_the_note():
    conn = _conn({"type": "task", "status": "in_progress"})
    with pytest.raises(InvalidTransitionError) as refused:
        TicketService().close_ticket(conn, SCHEMA, 1, resolution=NOTE)
    message = str(refused.value)
    assert NOTE not in message and "Toteutettu" not in message
    assert "done" in message and "cancelled" in message
    assert "resolution" in message and "append" in message


def test_batch_close_refusal_names_valid_terminals_without_echoing_the_note():
    conn = _conn({"type": "bug", "status": "triage"})
    result = TicketService().batch_close(conn, SCHEMA, [1], resolution=NOTE, confirm=True)
    error = result.results[0].error
    assert result.results[0].success is False
    assert NOTE not in error and "Toteutettu" not in error
    assert "resolved" in error and "wont_fix" in error
    assert "resolution" in error and "append" in error


def test_a_valid_terminal_still_closes():
    from tests.test_batch_operations import FIXED_TIME, _make_ticket_row

    conn = _conn(
        {"type": "task", "status": "in_progress"},
        _make_ticket_row(status="in_progress", type="task"),
        _make_ticket_row(status="done", type="task", closed_at=FIXED_TIME),
    )
    assert TicketService().close_ticket(conn, SCHEMA, 1, resolution="done").status == "done"
