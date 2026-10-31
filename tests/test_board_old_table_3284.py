"""The named-column board tolerates a tickets table that predates later columns (STOMPY-3284).

0.8.24 replaced the board's SELECT * with named columns. created_by
(migration N+5 / host post-baseline delta) and claimed_by / claimed_at /
claimed_until (host ticket_lease_delta) are later additions, so on a schema
whose table predates them the board failed with UndefinedColumn
"claimed_by" where SELECT * had worked (host merge-queue integration test
test_board_lists_the_granted_schema_not_the_decoy). The board reads the
table's columns once per (database, schema) and selects an absent one as NULL.
"""

from unittest.mock import MagicMock, patch

import pytest

from stompy_ticketing import board_view as bv
from stompy_ticketing.service import TicketService
from tests.board_fake import board_conn
from tests.test_service import _make_ticket_row

SCHEMA = "test_project"
#: The plugin's own tickets DDL plus migration N's archived_at: no created_by, no lease.
OLD_TABLE = frozenset({
    "id", "session_id", "title", "description", "type", "status", "priority",
    "assignee", "tags", "metadata", "created_at", "updated_at", "closed_at",
    "archived_at", "content_hash", "content_tsvector",
})
FULL_TABLE = OLD_TABLE | {"created_by", "claimed_by", "claimed_at", "claimed_until"}


def _service():
    svc = TicketService()
    svc.archive_stale_tickets = MagicMock(return_value=0)
    return svc


def _catalog_reads(cur):
    return [t for t, _ in cur.executed if "information_schema.columns" in t]


@pytest.mark.parametrize("fields", ["card", "full"])
def test_board_reads_a_table_without_the_lease_columns(fields):
    conn, cur = board_conn([_make_ticket_row(id=7)], columns=OLD_TABLE)

    board = _service().board_view(conn, SCHEMA, view="kanban", fields=fields)

    card = board.columns[0].tickets[0]
    assert card.id == 7 and card.claimed_by is None and card.created_by is None
    text, _ = cur.row_query()
    for name in ("created_by", "claimed_by", "claimed_at", "claimed_until"):
        assert f"NULL AS {name}" in text, name


def test_compact_board_reads_a_table_without_the_lease_columns():
    conn, _ = board_conn([_make_ticket_row(id=7)], columns=OLD_TABLE)

    board = _service().board_view(conn, SCHEMA, view="compact")

    assert board.columns[0].compact_tickets[0].id == 7


def test_a_current_table_names_every_column():
    conn, cur = board_conn([_make_ticket_row()], columns=FULL_TABLE)

    _service().board_view(conn, SCHEMA, view="kanban")

    assert "NULL AS" not in cur.row_query()[0]


def test_catalog_is_read_once_for_a_current_table():
    dsn = {"host": "h", "port": "5432", "dbname": "cache-current"}
    with patch.object(bv, "_table_columns", {}):
        conn, first = board_conn([], dsn=dsn, columns=FULL_TABLE)
        _service().board_view(conn, SCHEMA)
        conn, second = board_conn([], dsn=dsn, columns=FULL_TABLE)
        _service().board_view(conn, SCHEMA)
        conn, other_schema = board_conn([], dsn=dsn, columns=FULL_TABLE)
        _service().board_view(conn, "other_schema")
    assert (len(_catalog_reads(first)), len(_catalog_reads(second))) == (1, 0)
    assert len(_catalog_reads(other_schema)) == 1


def test_catalog_read_is_parameterized():
    conn, cur = board_conn([], columns=FULL_TABLE)

    _service().board_view(conn, SCHEMA)

    (text, params), = [(t, p) for t, p in cur.executed if "information_schema" in t]
    assert SCHEMA not in text and params == [SCHEMA, "tickets"]


def test_an_old_table_is_read_again_after_the_window():
    """A migration that adds the columns mid-process is picked up."""
    dsn = {"host": "h", "port": "5432", "dbname": "cache-old"}
    with patch.object(bv, "_table_columns", {}), patch.object(bv.time, "monotonic") as clock:
        clock.return_value = 1000.0
        _service().board_view(board_conn([], dsn=dsn, columns=OLD_TABLE)[0], SCHEMA)
        conn, within = board_conn([], dsn=dsn, columns=OLD_TABLE)
        _service().board_view(conn, SCHEMA)
        clock.return_value = 1000.0 + bv.SWEEP_INTERVAL_SECONDS
        conn, after = board_conn([], dsn=dsn, columns=FULL_TABLE)
        _service().board_view(conn, SCHEMA)
    assert len(_catalog_reads(within)) == 0
    assert len(_catalog_reads(after)) == 1
    assert "NULL AS" not in after.row_query()[0]


def test_an_unreadable_catalog_names_every_column_as_before():
    conn, cur = board_conn([_make_ticket_row()])  # catalog answers nothing

    _service().board_view(conn, SCHEMA)

    assert "NULL AS" not in cur.row_query()[0]
