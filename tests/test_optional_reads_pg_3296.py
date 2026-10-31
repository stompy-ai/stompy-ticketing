"""STOMPY-3296 on a REAL Postgres: the reads that tolerate an older schema
leave the connection usable.

Skipped unless STOMPY_TICKETING_TEST_PG_DSN names a throwaway database (the
schema below is dropped and recreated). CI has no Postgres; the fake twin is
tests/test_optional_reads_3296.py.
"""

import os
import time

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

from stompy_ticketing.schema import get_all_ticket_tables_sql  # noqa: E402
from stompy_ticketing.service import TicketService  # noqa: E402

DSN = os.environ.get("STOMPY_TICKETING_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="STOMPY_TICKETING_TEST_PG_DSN not set")
SCHEMA = "t3296_no_context_links"


@pytest.fixture
def conn():
    c = psycopg2.connect(DSN, cursor_factory=RealDictCursor)
    with c.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        cur.execute(f'CREATE SCHEMA "{SCHEMA}"')
        cur.execute(f'CREATE TABLE "{SCHEMA}".sessions (id TEXT PRIMARY KEY)')
        cur.execute(get_all_ticket_tables_sql(SCHEMA))
        # A schema that predates migration 32.
        cur.execute(f'DROP TABLE "{SCHEMA}".ticket_context_links')
        now = time.time()
        for title in ("one", "two"):
            cur.execute(
                f'INSERT INTO "{SCHEMA}".tickets (title, type, status, created_at, updated_at) '
                "VALUES (%s, 'task', 'backlog', %s, %s)",
                (title, now, now),
            )
        cur.execute(
            f'INSERT INTO "{SCHEMA}".ticket_links (source_id, target_id, link_type, created_at) '
            "VALUES (1, 2, 'blocks', %s)",
            (now,),
        )
    c.commit()
    yield c
    c.rollback()
    with c.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
    c.commit()
    c.close()


@pytest.mark.parametrize("fields", ["card", "full"])
def test_two_boards_on_one_connection_both_answer(conn, fields):
    svc = TicketService()
    first = svc.board_view(conn, SCHEMA, fields=fields)
    second = svc.board_view(conn, SCHEMA, fields=fields)

    for board in (first, second):
        cards = {t.id: t for col in board.columns for t in col.tickets}
        assert set(cards) == {1, 2}
        assert len(cards[1].links) == 1 and cards[1].context_links == []
    assert conn.get_transaction_status() != psycopg2.extensions.TRANSACTION_STATUS_INERROR


def test_get_ticket_then_a_board_on_one_connection(conn):
    svc = TicketService()
    ticket = svc.get_ticket(conn, SCHEMA, 1)
    board = svc.board_view(conn, SCHEMA)

    assert ticket.context_links == [] and len(ticket.links) == 1
    assert board.total == 2
