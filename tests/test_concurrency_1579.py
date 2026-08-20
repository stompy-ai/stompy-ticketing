"""STOMPY-1579: parallel agents must not clobber each other's ticket text.

Two sessions worked one journey ticket in parallel on 2026-08-19; session B
re-scoped the description, session A (holding the old text) wrote its RESULT
via `update` — B's re-scope vanished silently, because update is a wholesale
replace of one big text field. Pinned here:

* `append_description` is ATOMIC in SQL (description || separator || text) —
  no read-modify-write, so concurrent appends both land;
* `update` accepts `expected_updated_at`; when the ticket moved since the
  caller's read, it refuses with a CONFLICT payload naming both timestamps
  instead of silently overwriting;
* without the guard, `update` behaves exactly as before (compat).
"""

from unittest.mock import MagicMock

import pytest

from stompy_ticketing.models import TicketUpdate
from stompy_ticketing.service import TicketService


def _cur_with_row(row):
    cur = MagicMock()
    cur.fetchone.return_value = row
    return cur


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


class TestAppendDescription:
    def test_append_is_one_atomic_update(self):
        svc = TicketService()
        cur = _cur_with_row(_row(description="original\n\nRESULT A"))
        conn = MagicMock()
        conn.cursor.return_value = cur
        out = svc.append_description(conn, "s", 7, "RESULT A", changed_by="agent-a")
        assert out is not None
        sqls = [str(c.args[0]) for c in cur.execute.call_args_list]
        # ONE update that concatenates in SQL — never a SELECT-then-replace
        assert any("COALESCE(description" in q and "|| %s" in q for q in sqls)
        assert not any(q.strip().upper().startswith("SELECT * FROM") for q in sqls)

    def test_append_records_history_without_duplicating_the_whole_text(self):
        svc = TicketService()
        cur = _cur_with_row(_row())
        conn = MagicMock()
        conn.cursor.return_value = cur
        svc.append_description(conn, "s", 7, "RESULT B")
        hist = [c for c in cur.execute.call_args_list if "ticket_history" in str(c.args[0])]
        assert len(hist) == 1
        params = hist[0].args[1]
        assert "description_appended" in params
        assert "RESULT B" in params  # the appended text, not the merged blob

    def test_append_missing_ticket_returns_none(self):
        svc = TicketService()
        cur = _cur_with_row(None)
        conn = MagicMock()
        conn.cursor.return_value = cur
        assert svc.append_description(conn, "s", 999, "x") is None


class TestOptimisticUpdate:
    def _service_with_current(self, updated_at=100.0):
        svc = TicketService()
        cur = MagicMock()
        cur.fetchone.side_effect = [_row(updated_at=updated_at), _row(description="new")]
        conn = MagicMock()
        conn.cursor.return_value = cur
        return svc, conn, cur

    def test_stale_expected_updated_at_refuses_with_conflict(self):
        svc, conn, cur = self._service_with_current(updated_at=200.0)
        with pytest.raises(TicketService.Conflict) as e:
            svc.update_ticket(
                conn, "s", 7, TicketUpdate(description="mine"),
                expected_updated_at=100.0,
            )
        assert e.value.current_updated_at == 200.0
        assert e.value.expected_updated_at == 100.0
        # nothing written
        assert not any("UPDATE" in str(c.args[0]) for c in cur.execute.call_args_list)

    def test_matching_expected_updated_at_proceeds(self):
        svc, conn, cur = self._service_with_current(updated_at=100.0)
        out = svc.update_ticket(
            conn, "s", 7, TicketUpdate(description="mine"), expected_updated_at=100.0
        )
        assert out is not None

    def test_no_guard_keeps_old_behaviour(self):
        svc, conn, cur = self._service_with_current(updated_at=200.0)
        out = svc.update_ticket(conn, "s", 7, TicketUpdate(description="mine"))
        assert out is not None
