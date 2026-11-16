"""STOMPY-2095: a refused close never echoes the caller's `resolution` text.

`resolution` names the terminal STATUS. Agents keep passing their closing note
there; the refusal used to quote that note in full ("'<note>' is not a terminal
status"), so a customer's prose landed verbatim in the host's error_events rows
(prod, 2026-11-13..15, six rows for one beta user).

Decided (Markus, 2026-11-16, option a): free text is what every one of those
agents meant, so it is the closing NOTE. The ticket closes with its type's
default terminal and the text is appended. A status WORD that is not a terminal
for this type is still a status mistake and is refused, without echo.
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


# --- (a), Markus 2026-11-16: free text is the closing NOTE, not a refusal ----------


def _svc(final_status):
    """A service whose walk and append are recorded, not run against SQL."""
    svc = TicketService()
    svc.transition_ticket = MagicMock(return_value=MagicMock(status=final_status))
    svc.append_description = MagicMock()
    return svc


def test_close_with_a_note_closes_with_the_default_terminal_and_keeps_the_note():
    svc = _svc("done")
    conn = _conn({"type": "task", "status": "in_progress"})
    result = svc.close_ticket(conn, SCHEMA, 1, resolution=NOTE, changed_by="51")
    assert result.status == "done"
    assert [c.args[3] for c in svc.transition_ticket.call_args_list] == ["done"]
    (call,) = svc.append_description.call_args_list
    text = call.args[3]
    assert NOTE in text and "done" in text
    assert call.kwargs["changed_by"] == "51"


def test_a_bug_closed_with_a_note_lands_on_resolved_not_wont_fix():
    svc = _svc("resolved")
    conn = _conn({"type": "bug", "status": "triage"})
    svc.close_ticket(conn, SCHEMA, 1, resolution="fixed in PR #12")
    assert svc.transition_ticket.call_args_list[-1].args[3] == "resolved"
    assert "fixed in PR #12" in svc.append_description.call_args.args[3]


def test_a_terminal_status_still_closes_without_a_note():
    svc = _svc("cancelled")
    conn = _conn({"type": "task", "status": "backlog"})
    svc.close_ticket(conn, SCHEMA, 1, resolution="cancelled")
    assert svc.transition_ticket.call_args_list[-1].args[3] == "cancelled"
    svc.append_description.assert_not_called()


def _says_how_to_leave_a_note(message, default):
    """STOMPY-3684: since 0.8.35 a sentence IS kept as the note, so the refusal
    must say so, and must not send agents to append-then-close any more."""
    assert "not a note" not in message and "append" not in message
    assert "sentence" in message and "closing note" in message and default in message


@pytest.mark.parametrize("token", ["wont_fix", "in_progress", "rejected", "WONT_FIX"])
def test_a_status_word_of_the_wrong_kind_is_still_refused_without_echo(token):
    """A status NAME is a status mistake, never a note: 'wont_fix' on a task must
    not quietly close it as 'done'."""
    svc = _svc("done")
    conn = _conn({"type": "task", "status": "backlog"})
    with pytest.raises(InvalidTransitionError) as refused:
        svc.close_ticket(conn, SCHEMA, 1, resolution=token)
    message = str(refused.value)
    assert "done" in message and "cancelled" in message
    _says_how_to_leave_a_note(message, "done")
    svc.transition_ticket.assert_not_called()
    svc.append_description.assert_not_called()


def test_a_note_too_long_to_keep_refuses_before_anything_changes():
    svc = _svc("done")
    conn = _conn({"type": "task", "status": "backlog"})
    long_note = "x" * (TicketService.MAX_APPEND_CHARS + 1)
    with pytest.raises(ValueError) as refused:
        svc.close_ticket(conn, SCHEMA, 1, resolution=long_note)
    assert long_note not in str(refused.value)
    svc.transition_ticket.assert_not_called()


def test_batch_close_with_a_note_closes_each_and_keeps_the_note():
    svc = _svc("resolved")
    conn = _conn({"type": "bug", "status": "triage"}, {"type": "task", "status": "backlog"})
    result = svc.batch_close(conn, SCHEMA, [1, 2], resolution=NOTE, confirm=True, changed_by="51")
    assert result.succeeded == 2 and result.failed == 0
    assert [r.new_status for r in result.results] == ["resolved", "done"]
    assert all(r.note_kept for r in result.results)
    assert len(svc.append_description.call_args_list) == 2
    assert all(NOTE in c.args[3] for c in svc.append_description.call_args_list)


def test_batch_close_preview_with_a_note_writes_nothing():
    svc = _svc("done")
    conn = _conn({"type": "task", "status": "backlog"})
    result = svc.batch_close(conn, SCHEMA, [1], resolution=NOTE, confirm=False)
    assert result.dry_run and result.results[0].new_status == "done"
    svc.transition_ticket.assert_not_called()
    svc.append_description.assert_not_called()


def test_batch_close_refuses_a_wrong_status_word_without_echo():
    svc = _svc("done")
    conn = _conn({"type": "task", "status": "backlog"})
    result = svc.batch_close(conn, SCHEMA, [1], resolution="wont_fix", confirm=True)
    error = result.results[0].error
    assert result.results[0].success is False
    assert "done" in error and "cancelled" in error
    _says_how_to_leave_a_note(error, "done")


def test_a_valid_terminal_still_closes():
    from tests.test_batch_operations import FIXED_TIME, _make_ticket_row

    conn = _conn(
        {"type": "task", "status": "in_progress"},
        _make_ticket_row(status="in_progress", type="task"),
        _make_ticket_row(status="done", type="task", closed_at=FIXED_TIME),
    )
    assert TicketService().close_ticket(conn, SCHEMA, 1, resolution="done").status == "done"


# --- the doors -------------------------------------------------------------------

from tests.test_not_found_envelope_1879 import _register, _run  # noqa: E402
from tests.test_rest_actor_2380 import CALLER, _ticket, door  # noqa: E402,F401


def test_mcp_close_says_the_note_was_kept_and_which_status_it_closed_as():
    ticket_fn = _register("close_ticket", return_value=_ticket(status="done"))
    answer = _run(ticket_fn(action="close", ticket_id=1, resolution=NOTE, project="test_project"))
    assert "status: closed" in answer
    assert "note_kept: true" in answer and "closed_as: done" in answer
    assert NOTE not in answer, "the note is on the ticket, not echoed back"


def test_mcp_close_with_a_terminal_status_says_nothing_about_a_note():
    ticket_fn = _register("close_ticket", return_value=_ticket(status="cancelled"))
    answer = _run(ticket_fn(action="close", ticket_id=1, resolution="cancelled",
                            project="test_project"))
    assert "status: closed" in answer and "note_kept" not in answer


def test_rest_batch_close_keeps_the_note_and_never_makes_it_the_actor(door):
    client, service, configure = door
    configure(lambda: CALLER)
    r = client.post("/projects/p/tickets/batch/close",
                    json={"ticket_ids": [1], "confirm": True, "note": NOTE})
    assert r.status_code == 200, r.text
    assert service.batch_close.call_args.kwargs["resolution"] == NOTE
    assert service.batch_close.call_args.kwargs["changed_by"] == CALLER
