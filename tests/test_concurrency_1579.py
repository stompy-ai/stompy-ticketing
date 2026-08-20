"""STOMPY-1579: parallel agents must not clobber each other's ticket text.

Two sessions worked one journey ticket in parallel on 2026-08-19; session B
re-scoped the description, session A (holding the old text) wrote its RESULT
via `update` — B's re-scope vanished silently, because update is a wholesale
replace of one big text field. Pinned here (post-Kimi #26, the atomic shape):

* `append_description` is ONE SQL statement (CASE concat, no read-modify-
  write) that bumps updated_at IN THE SAME STATEMENT — so an append followed
  by a stale guarded update conflicts instead of erasing the append;
* the optimistic guard is a compare-and-swap IN THE UPDATE'S WHERE CLAUSE —
  never a Python-side check-then-act (that loses the very race it exists to
  close); zero rows under the guard re-reads once to distinguish 404 from
  Conflict, and rolls back;
* without the guard, `update` behaves exactly as before (compat);
* empty and oversized appends are refused (a looping agent must not grow a
  row without bound).
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


def _sqls(cur):
    return [str(c.args[0]) for c in cur.execute.call_args_list if c.args]


class TestAppendDescription:
    def test_append_is_one_atomic_update_that_bumps_updated_at(self):
        svc = TicketService()
        cur = MagicMock()
        cur.fetchone.return_value = _row(description="original\n\nRESULT A")
        conn = MagicMock()
        conn.cursor.return_value = cur
        out = svc.append_description(conn, "s", 7, "RESULT A", changed_by="agent-a")
        assert out is not None
        stmts = _sqls(cur)
        update = next(q for q in stmts if "UPDATE" in q)
        # concat in SQL (CASE avoids a leading separator on empty descriptions)
        assert "CASE" in update and "|| %s" in update
        # updated_at bumped in the SAME statement — a later stale guarded
        # update must conflict rather than erase this append
        assert "updated_at = %s" in update
        # never a read-modify-write of the description
        assert not any("SELECT" in q and "description" in q for q in stmts)

    def test_append_records_history_with_the_appended_text_only(self):
        svc = TicketService()
        cur = MagicMock()
        cur.fetchone.return_value = _row()
        conn = MagicMock()
        conn.cursor.return_value = cur
        svc.append_description(conn, "s", 7, "RESULT B")
        hist = [c for c in cur.execute.call_args_list if c.args and "ticket_history" in str(c.args[0])]
        assert len(hist) == 1
        params = hist[0].args[1]
        assert "description_appended" in params and "RESULT B" in params

    def test_append_missing_ticket_returns_none(self):
        svc = TicketService()
        cur = MagicMock()
        cur.fetchone.return_value = None
        conn = MagicMock()
        conn.cursor.return_value = cur
        assert svc.append_description(conn, "s", 999, "x") is None

    def test_append_refuses_empty_and_oversized_text(self):
        svc = TicketService()
        conn = MagicMock()
        with pytest.raises(ValueError):
            svc.append_description(conn, "s", 7, "   ")
        with pytest.raises(ValueError):
            svc.append_description(conn, "s", 7, "x" * (TicketService.MAX_APPEND_CHARS + 1))
        assert not conn.cursor.called  # refused before any SQL


class TestOptimisticUpdate:
    def _run(self, fetches):
        svc = TicketService()
        cur = MagicMock()
        cur.fetchone.side_effect = fetches
        conn = MagicMock()
        conn.cursor.return_value = cur
        return svc, conn, cur

    def test_guard_lives_in_the_update_where_clause(self):
        """The compare-and-swap must be enforced BY THE DATABASE — a Python
        compare between SELECT and UPDATE is the race, not the fix."""
        svc, conn, cur = self._run([_row(updated_at=100.0), _row(description="mine")])
        out = svc.update_ticket(
            conn, "s", 7, TicketUpdate(description="mine"), expected_updated_at=100.0
        )
        assert out is not None
        update = next(q for q in _sqls(cur) if "UPDATE" in q)
        assert "AND updated_at = %s" in update
        # the guard value rides the UPDATE's parameters
        upd_call = next(c for c in cur.execute.call_args_list if c.args and "UPDATE" in str(c.args[0]))
        assert 100.0 in tuple(upd_call.args[1])

    def test_zero_rows_under_guard_raises_conflict_with_current_timestamp(self):
        svc, conn, cur = self._run(
            [_row(updated_at=200.0), None, {"updated_at": 200.0}]
        )
        with pytest.raises(TicketService.Conflict) as e:
            svc.update_ticket(
                conn, "s", 7, TicketUpdate(description="mine"), expected_updated_at=100.0
            )
        assert e.value.current_updated_at == 200.0
        assert e.value.expected_updated_at == 100.0
        conn.rollback.assert_called()  # no transaction left open

    def test_zero_rows_under_guard_for_a_deleted_ticket_returns_none(self):
        svc, conn, cur = self._run([_row(updated_at=100.0), None, None])
        out = svc.update_ticket(
            conn, "s", 7, TicketUpdate(description="mine"), expected_updated_at=50.0
        )
        assert out is None

    def test_no_guard_keeps_old_behaviour(self):
        svc, conn, cur = self._run([_row(updated_at=200.0), _row(description="mine")])
        out = svc.update_ticket(conn, "s", 7, TicketUpdate(description="mine"))
        assert out is not None
        update = next(q for q in _sqls(cur) if "UPDATE" in q)
        assert "AND updated_at" not in update
