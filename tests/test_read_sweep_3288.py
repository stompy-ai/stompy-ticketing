"""ticket list and ticket search share the board's sweep throttle, and the
sweep is one statement however many tickets it archives (STOMPY-3288).

Before: list_tickets and search_tickets ran archive_stale_tickets (a write) on
every read, and the sweep issued SELECT + UPDATE + one history INSERT per stale
ticket. The board alone was throttled (0.8.24, STOMPY-3284).

Each test reads on its own (host, port, dbname), so the process-wide window
never carries over between tests.
"""

from unittest.mock import MagicMock

from stompy_ticketing.service import TicketService
from tests.test_service import FIXED_TIME, _mock_conn_and_cursor, _sql_to_str

SCHEMA = "test_project"


def _service():
    svc = TicketService()
    svc.archive_stale_tickets = MagicMock(return_value=0)
    return svc


def _read_conn(database):
    conn, cur = _mock_conn_and_cursor()
    conn.get_dsn_parameters.return_value = {
        "host": f"t3288-{database}", "port": "5432", "dbname": "d",
    }
    cur.fetchone.return_value = {"count": 0}
    cur.fetchall.return_value = []
    return conn


class TestListAndSearchShareTheBoardThrottle:
    def test_second_list_within_the_window_does_not_sweep(self):
        svc = _service()
        svc.list_tickets(_read_conn("list-twice"), SCHEMA)
        svc.list_tickets(_read_conn("list-twice"), SCHEMA)
        assert svc.archive_stale_tickets.call_count == 1

    def test_second_search_within_the_window_does_not_sweep(self):
        svc = _service()
        svc.search_tickets(_read_conn("search-twice"), SCHEMA, "sweep")
        svc.search_tickets(_read_conn("search-twice"), SCHEMA, "sweep")
        assert svc.archive_stale_tickets.call_count == 1

    def test_board_list_and_search_share_one_window(self):
        """One helper, one window: a board read suppresses the next list and
        search on the same (database, schema), and vice versa."""
        svc = _service()
        svc.board_view(_read_conn("shared"), SCHEMA, view="summary")
        svc.list_tickets(_read_conn("shared"), SCHEMA)
        svc.search_tickets(_read_conn("shared"), SCHEMA, "sweep")
        assert svc.archive_stale_tickets.call_count == 1

    def test_list_window_is_per_schema(self):
        svc = _service()
        svc.list_tickets(_read_conn("per-schema"), SCHEMA)
        svc.list_tickets(_read_conn("per-schema"), "other_schema")
        assert svc.archive_stale_tickets.call_count == 2

    def test_a_failing_sweep_never_fails_list_or_search(self):
        svc = _service()
        svc.archive_stale_tickets.side_effect = RuntimeError("read-only replica")
        listed = svc.list_tickets(_read_conn("failing-list"), SCHEMA)
        found = svc.search_tickets(_read_conn("failing-search"), SCHEMA, "sweep")
        assert listed.total == 0
        assert found.total == 0


class TestSweepIsOneStatement:
    def _sweep(self, stale_ids):
        from unittest.mock import patch

        conn, cur = _mock_conn_and_cursor()
        # Rows shaped for both the old SELECT and the new RETURNING, so the
        # assertion (not a KeyError) is what fails on the old sweep.
        cur.fetchall.return_value = [
            {"id": i, "ticket_id": i, "type": "task", "status": "done"}
            for i in stale_ids
        ]
        with patch("stompy_ticketing.service.time") as clock:
            clock.time.return_value = FIXED_TIME + 2_000_000
            count = TicketService().archive_stale_tickets(conn, SCHEMA)
        return conn, cur, count

    def test_five_stale_tickets_are_archived_in_one_statement(self):
        conn, cur, count = self._sweep([1, 2, 3, 4, 5])
        assert count == 5
        assert cur.execute.call_count == 1
        conn.commit.assert_called_once()

    def test_the_statement_updates_and_writes_history_parameterized(self):
        _, cur, _ = self._sweep([7])
        query, params = cur.execute.call_args_list[0][0]
        text = _sql_to_str(query)
        assert "UPDATE test_project.tickets" in text
        assert "INSERT INTO test_project.ticket_history" in text
        assert "RETURNING" in text
        assert "system:auto_archive" not in text  # a parameter, never inlined
        assert "system:auto_archive" in params
        assert "archived_at" in params
