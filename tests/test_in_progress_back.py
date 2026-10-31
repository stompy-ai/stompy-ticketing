"""in_progress has a non-terminal way back.

A ticket that stalled in in_progress could only be closed or left there:
every type's in_progress row listed terminals only. Each type with an
in_progress state now steps back to the state that hands work to it
(task → backlog, bug → confirmed, feature → approved). decision has no
in_progress state, so it has nothing to add.

RED first: written before the transitions existed; the move refused with
"Allowed next: [...]".
"""

from unittest.mock import patch

import pytest

from stompy_ticketing.service import (
    STATE_MACHINES,
    InvalidTransitionError,
    TicketService,
    find_transition_path,
    validate_transition,
)
from tests.test_service import (
    FIXED_TIME,
    SCHEMA,
    _make_ticket_row,
    _mock_conn_and_cursor,
    _sql_to_str,
)

STEP_BACK = [
    ("task", "backlog"),
    ("bug", "confirmed"),
    ("feature", "approved"),
]


@pytest.mark.parametrize("ticket_type,back_to", STEP_BACK)
def test_in_progress_steps_back(ticket_type, back_to):
    assert validate_transition(ticket_type, "in_progress", back_to) is True


@pytest.mark.parametrize("ticket_type,back_to", STEP_BACK)
def test_step_back_target_hands_work_to_in_progress(ticket_type, back_to):
    """The way back lands on a state whose next step is in_progress again."""
    assert "in_progress" in STATE_MACHINES[ticket_type]["transitions"][back_to]


def test_existing_in_progress_exits_are_kept():
    assert STATE_MACHINES["task"]["transitions"]["in_progress"][:2] == ["done", "cancelled"]
    assert STATE_MACHINES["bug"]["transitions"]["in_progress"][:2] == ["resolved", "wont_fix"]
    assert STATE_MACHINES["feature"]["transitions"]["in_progress"][:2] == ["shipped", "rejected"]


def test_decision_has_no_in_progress():
    assert "in_progress" not in STATE_MACHINES["decision"]["transitions"]


def test_every_in_progress_has_a_non_terminal_exit():
    for ticket_type, sm in STATE_MACHINES.items():
        exits = sm["transitions"].get("in_progress")
        if exits is None:
            continue
        assert [s for s in exits if s not in sm["terminal"]], (
            f"{ticket_type}.in_progress only exits to terminals: {exits}"
        )


def test_task_in_progress_to_terminal_is_still_one_step():
    """close() walks the shortest path; adding a way back must not lengthen it."""
    assert find_transition_path("task", "in_progress", "done") == ["done"]
    assert find_transition_path("bug", "in_progress", "resolved") == ["resolved"]


def test_in_progress_park_names_the_path_through_the_step_back():
    """in_progress still cannot park directly; the refusal now teaches the way."""
    assert find_transition_path("task", "in_progress", "parked") == ["backlog", "parked"]
    with pytest.raises(InvalidTransitionError, match="Path: backlog → parked"):
        validate_transition("task", "in_progress", "parked")


@patch("stompy_ticketing.service.time")
def test_move_in_progress_to_backlog_records_history(mock_time):
    mock_time.time.return_value = FIXED_TIME
    current = _make_ticket_row(status="in_progress", type="task")
    updated = _make_ticket_row(status="backlog", type="task")
    conn, cur = _mock_conn_and_cursor()
    cur.fetchone.side_effect = [current, updated]

    result = TicketService().transition_ticket(conn, SCHEMA, 1, "backlog", changed_by="tester")

    assert result.status == "backlog"
    assert result.closed_at is None
    conn.commit.assert_called_once()
    history = [
        c for c in cur.execute.call_args_list
        if "ticket_history" in _sql_to_str(c[0][0]) and "INSERT" in _sql_to_str(c[0][0])
    ]
    assert len(history) == 1
    assert history[0][0][1] == (1, "status", "in_progress", "backlog", "tester", FIXED_TIME)
