"""An optional read never leaves the caller's transaction aborted (STOMPY-3296).

board_view and get_ticket caught exceptions around three reads and carried
on: the archived COUNT, the context-link fetch ("table may not exist yet on
older schemas") and the board's link bulk fetch. On Postgres a failed
statement aborts the transaction, nothing rolled back, so the read answered
and the NEXT statement on that connection failed with "current transaction
is aborted". MagicMock cursors hid it: they never abort.

Now the only absence the plugin tolerates, a schema that predates
ticket_context_links (migration 32), is asked of the catalog once per
(database, schema), and the context-link read is skipped there instead of
failing. Every other failure propagates with its own error to the door's
catch-all, which answers it, instead of being swallowed into a poisoned
connection. The fake below aborts the way Postgres does; the real-PG twin is
tests/test_optional_reads_pg_3296.py.
"""

from unittest.mock import MagicMock, patch

import psycopg2.errors
import pytest

from stompy_ticketing import board_view as bv
from stompy_ticketing.service import TicketService
from tests.board_fake import FakeBoardCursor, board_conn
from tests.test_service import FIXED_TIME, _make_ticket_row

SCHEMA = "test_project"
CTX = "ticket_context_links"


class AbortingCursor(FakeBoardCursor):
    """FakeBoardCursor with Postgres' transaction semantics: a failed
    statement aborts the transaction until conn.rollback()."""

    def __init__(self, rows, missing=(), fail_on=None, **kw):
        super().__init__(rows, **kw)
        self.missing = set(missing)  # tables this schema predates
        self.fail_on = fail_on  # (substring, exception): a genuine failure
        self.aborted = False

    def _execute(self, query, params=None):
        from tests.test_service import _sql_to_str

        text = _sql_to_str(query)
        if self.aborted:
            self.executed.append((text, list(params or [])))
            raise psycopg2.errors.InFailedSqlTransaction(
                "current transaction is aborted, commands ignored until end of transaction block"
            )
        for table in self.missing:
            if f".{table}" in text:
                self.executed.append((text, list(params or [])))
                self.aborted = True
                raise psycopg2.errors.UndefinedTable(f'relation "{SCHEMA}.{table}" does not exist')
        if self.fail_on and self.fail_on[0] in text:
            self.executed.append((text, list(params or [])))
            self.aborted = True
            raise self.fail_on[1]
        if "pg_catalog.pg_tables" in text:
            self.executed.append((text, list(params or [])))
            self._one = {"present": list(params)[1] not in self.missing}
            self._result = [self._one]
            return
        super()._execute(query, params)
        if "WHERE id = %s" in text and _is_ticket_read(text):
            wanted = list(params or [])[0]
            self._one = next((r for r in self.rows if r["id"] == wanted), None)

    def rollback(self):
        self.aborted = False


def _is_ticket_read(text):
    from tests.board_fake import _is_ticket_read as read

    return read(text)


def pg_conn(rows, missing=(), fail_on=None, link_rows=None, dsn=None):
    conn, _ = board_conn(rows, dsn=dsn)
    cur = AbortingCursor(rows, missing=missing, fail_on=fail_on, link_rows=link_rows)
    conn.cursor.return_value = cur
    conn.rollback.side_effect = cur.rollback
    return conn, cur


def _service():
    svc = TicketService()
    svc.archive_stale_tickets = MagicMock(return_value=0)
    return svc


def _link(link_id, src, tgt):
    return {
        "id": link_id, "source_id": src, "target_id": tgt, "link_type": "blocks",
        "created_at": FIXED_TIME, "source_title": f"T{src}", "source_status": "backlog",
        "target_title": f"T{tgt}", "target_status": "backlog",
    }


@pytest.mark.parametrize("fields", ["card", "full"])
def test_two_boards_on_one_connection_without_context_links_both_answer(fields):
    conn, cur = pg_conn([_make_ticket_row(id=1)], missing={CTX})
    svc = _service()

    first = svc.board_view(conn, SCHEMA, fields=fields)
    second = svc.board_view(conn, SCHEMA, fields=fields)

    assert [t.id for t in first.columns[0].tickets] == [1]
    assert [t.id for t in second.columns[0].tickets] == [1]
    assert not cur.aborted


def test_a_board_without_context_links_never_queries_the_missing_table():
    """No failed statement at all: the catalog answers, the read is skipped,
    and the ticket links still arrive."""
    conn, cur = pg_conn(
        [_make_ticket_row(id=1), _make_ticket_row(id=2)], missing={CTX},
        link_rows={"ticket_links": [_link(10, 1, 2)]},
    )

    board = _service().board_view(conn, SCHEMA, fields="full")

    assert not [t for t, _ in cur.executed if f".{CTX}" in t]
    cards = {t.id: t for t in board.columns[0].tickets}
    assert [l.peer_id for l in cards[1].links] == [2]
    assert cards[1].context_links == []


def test_get_ticket_without_context_links_leaves_the_connection_usable():
    conn, cur = pg_conn([_make_ticket_row(id=1)], missing={CTX})
    svc = _service()

    ticket = svc.get_ticket(conn, SCHEMA, 1)
    board = svc.board_view(conn, SCHEMA)  # the next statement on that conn

    assert ticket.id == 1 and ticket.context_links == []
    assert [t.id for t in board.columns[0].tickets] == [1]
    assert not cur.aborted


def test_a_genuine_link_failure_is_raised_not_swallowed_into_an_aborted_transaction():
    """A timeout on the link fetch used to return a board with no links and a
    poisoned connection; now the board fails with the driver's own error,
    which the door's catch-all answers (BOARD_UNAVAILABLE)."""
    timeout = psycopg2.errors.QueryCanceled("canceling statement due to statement timeout")
    conn, _ = pg_conn([_make_ticket_row(id=1)], fail_on=(".ticket_links", timeout))

    with pytest.raises(psycopg2.errors.QueryCanceled):
        _service().board_view(conn, SCHEMA)


def test_a_failed_archived_count_raises_its_own_error():
    """The archived COUNT's `except: pass` turned its failure into the NEXT
    statement's InFailedSqlTransaction: the cause was lost."""
    boom = psycopg2.errors.UndefinedColumn('column "archived_at" does not exist')
    conn, _ = pg_conn([_make_ticket_row(id=1)], fail_on=("archived_at IS NOT NULL", boom))

    with pytest.raises(psycopg2.errors.UndefinedColumn):
        _service().board_view(conn, SCHEMA, view="summary")


def _catalog_reads(cur):
    return [t for t, _ in cur.executed if "pg_catalog.pg_tables" in t]


def test_the_context_links_table_is_looked_up_once_when_present():
    dsn = {"host": "h", "port": "5432", "dbname": "ctx-present"}
    with patch.object(bv, "_context_links_present", {}):
        conn, first = pg_conn([_make_ticket_row(id=1)], dsn=dsn)
        _service().board_view(conn, SCHEMA)
        conn, second = pg_conn([_make_ticket_row(id=1)], dsn=dsn)
        _service().get_ticket(conn, SCHEMA, 1)
    assert (len(_catalog_reads(first)), len(_catalog_reads(second))) == (1, 0)


def test_an_absent_context_links_table_is_looked_up_again_after_the_window():
    """Migration 32 landing mid-process is picked up."""
    dsn = {"host": "h", "port": "5432", "dbname": "ctx-absent"}
    rows = [_make_ticket_row(id=1)]
    ctx = {"context_links": [{
        "id": 5, "ticket_id": 1, "context_label": "a_label", "context_version": "latest",
        "link_type": "references", "created_at": FIXED_TIME,
        "ticket_title": "T1", "ticket_status": "backlog",
    }]}
    with patch.object(bv, "_context_links_present", {}), \
            patch.object(bv.time, "monotonic") as clock:
        clock.return_value = 1000.0
        _service().board_view(pg_conn(rows, missing={CTX}, dsn=dsn)[0], SCHEMA)
        conn, within = pg_conn(rows, missing={CTX}, dsn=dsn)
        _service().board_view(conn, SCHEMA)
        clock.return_value = 1000.0 + bv.SWEEP_INTERVAL_SECONDS
        conn, after = pg_conn(rows, dsn=dsn, link_rows=ctx)
        board = _service().board_view(conn, SCHEMA)
    assert len(_catalog_reads(within)) == 0
    assert len(_catalog_reads(after)) == 1
    assert [c.context_label for c in board.columns[0].tickets[0].context_links] == ["a_label"]


def test_the_catalog_lookup_is_parameterized():
    conn, cur = pg_conn([_make_ticket_row(id=1)])

    _service().board_view(conn, SCHEMA)

    (text, params), = [(t, p) for t, p in cur.executed if "pg_catalog.pg_tables" in t]
    assert SCHEMA not in text and params == [SCHEMA, CTX]
