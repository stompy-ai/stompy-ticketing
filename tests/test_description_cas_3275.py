"""STOMPY-3275: an update that writes the description cannot erase a concurrent append.

The 2394 guard (0.8.22) compares the new text with a description read by a plain SELECT;
the UPDATE then writes unconditionally. Agent B's atomic append landing between A's read
and A's write was silently erased (pre-tag review of 6.6.29). The description write is now
a compare-and-swap on the snapshot the guard judged: zero rows means the text moved, and
the caller is told to re-read (Conflict), whatever `expected_updated_at` / `confirm` say.
"""

from unittest.mock import MagicMock

import pytest

from stompy_ticketing.models import TicketUpdate
from stompy_ticketing.service import TicketService


def _row(**over):
    base = {
        "id": 7, "title": "t", "description": "original", "type": "task",
        "status": "backlog", "priority": "medium", "assignee": None,
        "tags": None, "metadata": None, "session_id": None,
        "content_hash": None, "created_at": 1.0, "updated_at": 100.0,
        "closed_at": None, "archived_at": None,
    }
    base.update(over)
    return base


def _update_call(cur):
    return next(c for c in cur.execute.call_args_list if c.args and "UPDATE" in str(c.args[0]))


def test_a_description_write_is_conditional_on_the_text_it_read():
    svc = TicketService()
    cur = MagicMock()
    cur.fetchone.side_effect = [_row(), _row(description="original + edit"), None]
    cur.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.return_value = cur
    svc.update_ticket(conn, "s", 7, TicketUpdate(description="original + edit"))
    call = _update_call(cur)
    assert "description IS NOT DISTINCT FROM %s" in str(call.args[0])
    assert call.args[1][-1] == "original"  # the snapshot the guard judged


def test_an_append_landing_between_read_and_write_is_a_conflict_not_an_erase():
    svc = TicketService()
    cur = MagicMock()
    # read sees "original"; the UPDATE matches no row (B appended); the re-read sees it moved
    cur.fetchone.side_effect = [_row(), None, {"updated_at": 200.0}]
    conn = MagicMock()
    conn.cursor.return_value = cur
    with pytest.raises(TicketService.Conflict):
        svc.update_ticket(conn, "s", 7, TicketUpdate(description="original + edit"))
    conn.rollback.assert_called()
    conn.commit.assert_not_called()


def test_a_confirmed_replace_is_conditional_too():
    svc = TicketService()
    cur = MagicMock()
    cur.fetchone.side_effect = [_row(), None, {"updated_at": 200.0}]
    conn = MagicMock()
    conn.cursor.return_value = cur
    with pytest.raises(TicketService.Conflict):
        svc.update_ticket(conn, "s", 7, TicketUpdate(description="brand new"), confirm=True)


def test_a_write_without_description_is_unchanged():
    svc = TicketService()
    cur = MagicMock()
    cur.fetchone.side_effect = [_row(), _row(priority="high"), None]
    cur.fetchall.return_value = []
    conn = MagicMock()
    conn.cursor.return_value = cur
    svc.update_ticket(conn, "s", 7, TicketUpdate(priority="high"))
    assert "description IS NOT DISTINCT FROM" not in str(_update_call(cur).args[0])
