"""ticket search backfills NULL tsvectors at most once per window (STOMPY-3290).

Before: search_tickets ran _backfill_null_tsvectors, an UPDATE, on every
search. The tsvector trigger (BEFORE INSERT OR UPDATE OF title, description)
fills content_tsvector on every create and edit, so NULL rows are only tickets
written before the trigger existed: after the first backfill the UPDATE is a
no-op write (and a scan: the GIN index is partial on IS NOT NULL) on every read.

Now the backfill runs through the same throttle as the stale sweep, under its
own key. A full batch re-arms the window, so a large pre-trigger backlog still
drains at one batch per search, as before.

Each test reads on its own (host, port, dbname), so the process-wide window
never carries over between tests.
"""

from unittest.mock import MagicMock, patch

from stompy_ticketing import stale_archive
from stompy_ticketing.service import TicketService
from tests.test_service import _mock_conn_and_cursor

SCHEMA = "test_project"
FULL_BATCH = 200  # _backfill_null_tsvectors' default limit


def _service(backfilled=0):
    svc = TicketService()
    svc.archive_stale_tickets = MagicMock(return_value=0)
    svc._backfill_null_tsvectors = MagicMock(return_value=backfilled)
    return svc


def _read_conn(database):
    conn, cur = _mock_conn_and_cursor()
    conn.get_dsn_parameters.return_value = {
        "host": f"t3290-{database}", "port": "5432", "dbname": "d",
    }
    cur.fetchone.return_value = {"count": 0}
    cur.fetchall.return_value = []
    return conn


class TestSearchBackfillIsThrottled:
    def test_second_search_within_the_window_does_not_backfill(self):
        svc = _service()
        svc.search_tickets(_read_conn("twice"), SCHEMA, "needle")
        svc.search_tickets(_read_conn("twice"), SCHEMA, "needle")
        assert svc._backfill_null_tsvectors.call_count == 1

    def test_search_after_the_window_backfills_again(self):
        svc = _service()
        window = stale_archive.SWEEP_INTERVAL_SECONDS
        with patch.object(stale_archive, "_clock") as clock:
            for now in (1000.0, 1000.0 + window - 1, 1000.0 + window):
                clock.return_value = now
                svc.search_tickets(_read_conn("window"), SCHEMA, "needle")
        assert svc._backfill_null_tsvectors.call_count == 2

    def test_a_full_batch_rearms_so_a_backlog_still_drains_per_search(self):
        svc = _service(backfilled=FULL_BATCH)
        svc.search_tickets(_read_conn("backlog"), SCHEMA, "needle")
        svc.search_tickets(_read_conn("backlog"), SCHEMA, "needle")
        assert svc._backfill_null_tsvectors.call_count == 2

    def test_the_backfill_window_is_its_own_not_the_sweeps(self):
        """A board read stamps the sweep's window; it must not suppress the
        first search's backfill on the same (database, schema)."""
        svc = _service()
        svc.board_view(_read_conn("own-key"), SCHEMA, view="summary")
        svc.search_tickets(_read_conn("own-key"), SCHEMA, "needle")
        svc.search_tickets(_read_conn("own-key"), SCHEMA, "needle")
        assert svc.archive_stale_tickets.call_count == 1
        assert svc._backfill_null_tsvectors.call_count == 1

    def test_window_is_per_schema(self):
        svc = _service()
        svc.search_tickets(_read_conn("per-schema"), SCHEMA, "needle")
        svc.search_tickets(_read_conn("per-schema"), "other_schema", "needle")
        assert svc._backfill_null_tsvectors.call_count == 2


class TestFailingBackfillLeavesTheSearchUsable:
    def test_a_failing_backfill_rolls_back_before_the_search_reads(self):
        """A failed UPDATE aborts the transaction; without a rollback the
        search's own SELECT fails with InFailedSqlTransaction on Postgres."""
        conn, cur = _mock_conn_and_cursor()
        cur.execute.side_effect = RuntimeError("read-only transaction")
        try:
            TicketService()._backfill_null_tsvectors(conn, SCHEMA)
        except RuntimeError:
            pass
        conn.rollback.assert_called_once()
