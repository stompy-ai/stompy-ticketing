"""STOMPY-3544 on a REAL Postgres: an assignee is an identity, not free text.

A ticket assigned 'Astra' was invisible to list(assignee='astra'), so a lead
polling its agent's queue silently lost an approval request for ~40 minutes
(2026-11-10). The list filter and claim_next both match case-insensitively; the
stored spelling is kept as written.

Skipped unless STOMPY_TICKETING_TEST_PG_DSN names a throwaway database."""

import os
import time

import pytest

psycopg2 = pytest.importorskip("psycopg2")
from psycopg2.extras import RealDictCursor  # noqa: E402

from stompy_ticketing import leases  # noqa: E402
from stompy_ticketing.models import TicketListFilters  # noqa: E402
from stompy_ticketing.schema import get_all_ticket_tables_sql  # noqa: E402
from stompy_ticketing.service import TicketService  # noqa: E402

DSN = os.environ.get("STOMPY_TICKETING_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="STOMPY_TICKETING_TEST_PG_DSN not set")
SCHEMA = "t3544_assignee_case"


@pytest.fixture
def conn():
    c = psycopg2.connect(DSN, cursor_factory=RealDictCursor)
    with c.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
        cur.execute(f'CREATE SCHEMA "{SCHEMA}"')
        cur.execute(f'CREATE TABLE "{SCHEMA}".sessions (id TEXT PRIMARY KEY)')
        cur.execute(get_all_ticket_tables_sql(SCHEMA))
        # The host's lease delta (dementia-production ticket_lease_delta.py) adds these.
        for col, kind in (
            ("claimed_by", "jsonb"),
            ("claimed_at", "double precision"),
            ("claimed_until", "double precision"),
        ):
            cur.execute(f'ALTER TABLE "{SCHEMA}".tickets ADD COLUMN IF NOT EXISTS {col} {kind}')
        now = time.time()
        for title, who in (("upper", "Astra"), ("lower", "astra"), ("other", "Claude")):
            cur.execute(
                f'INSERT INTO "{SCHEMA}".tickets (title, type, status, assignee, created_at,'
                " updated_at) VALUES (%s, 'task', 'backlog', %s, %s, %s)",
                (title, who, now, now),
            )
    c.commit()
    yield c
    c.rollback()
    with c.cursor() as cur:
        cur.execute(f'DROP SCHEMA IF EXISTS "{SCHEMA}" CASCADE')
    c.commit()
    c.close()


def _titles(conn, who):
    out = TicketService().list_tickets(conn, SCHEMA, TicketListFilters(assignee=who))
    rows = out if isinstance(out, list) else getattr(out, "tickets", out)
    return sorted(t.title for t in rows)


@pytest.mark.parametrize("who", ["astra", "Astra", "ASTRA", " astra "])
def test_list_matches_assignee_whatever_its_case_3544(conn, who):
    assert _titles(conn, who) == ["lower", "upper"]


def test_list_still_separates_different_assignees_3544(conn):
    assert _titles(conn, "claude") == ["other"]


def test_claim_next_finds_a_ticket_assigned_in_another_case_3544(conn):
    claimed = leases.claim_next(conn, SCHEMA, "ASTRA", {"agent_label": "astra"})
    row = claimed[0] if isinstance(claimed, tuple) else claimed
    assert row is not None and row["assignee"] in ("Astra", "astra")


def test_the_stored_spelling_is_kept_3544(conn):
    with conn.cursor() as cur:
        cur.execute(f'SELECT assignee FROM "{SCHEMA}".tickets ORDER BY id')
        assert [r["assignee"] for r in cur.fetchall()] == ["Astra", "astra", "Claude"]
