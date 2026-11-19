"""Read-back proof for bounded cards on Astra's disposable PostgreSQL."""

import json
from unittest.mock import patch

import pytest

from stompy_ticketing import board_view
from stompy_ticketing.models import TicketListFilters
from stompy_ticketing.schema import get_tickets_table_sql
from stompy_ticketing.service import TicketService
from tests.test_ticket_grep_462 import SCHEMA, native_db  # noqa: F401


@pytest.fixture(autouse=True)
def uncached_columns():
    with patch.dict(board_view._table_columns, {}, clear=True):
        yield


@pytest.mark.parametrize(
    "body",
    [None, "", "é" * 100, "😀" * 103, "😀" * 104, "x" * 100000],
    ids=["null", "empty", "100unicode", "103unicode", "104unicode", "100k"],
)
def test_native_card_matches_old_preview_and_preserves_full_get(native_db, body):
    with native_db.cursor() as cur:
        cur.execute(
            "UPDATE grep_462.tickets SET description=%s, metadata=%s WHERE id=1",
            (body, json.dumps({"reporter_email": "fixture@example.invalid"})),
        )
    service = TicketService()
    filters = TicketListFilters(limit=1)
    full = service.list_tickets(native_db, SCHEMA, filters, fields="full")
    expected = service.to_card(full.tickets[0])
    card = service.list_tickets(native_db, SCHEMA, filters).tickets[0]
    assert (
        card.description is None
        and card.description_preview == expected.description_preview
    )
    assert card.description_chars == len(body or "")
    assert card.metadata == full.tickets[0].metadata
    assert (
        service.get_ticket(
            native_db, SCHEMA, 1, include_history=False, include_links=False
        ).description
        == body
    )


def test_native_card_preserves_new_optional_columns(native_db):
    with native_db.cursor() as cur:
        cur.execute(
            "ALTER TABLE grep_462.tickets ADD COLUMN created_by text, "
            "ADD COLUMN claimed_by jsonb, ADD COLUMN claimed_at double precision, "
            "ADD COLUMN claimed_until double precision"
        )
        cur.execute(
            "UPDATE grep_462.tickets SET created_by='51', "
            "claimed_by=%s,claimed_at=1700000000,claimed_until=4102444800 WHERE id=1",
            (json.dumps({"account_id": 51, "agent_label": "fixture"}),),
        )
    card = (
        TicketService()
        .list_tickets(native_db, SCHEMA, TicketListFilters(limit=1))
        .tickets[0]
    )
    assert card.created_by == "51"
    assert card.claimed_by == {"account_id": 51, "agent_label": "fixture"}
    assert card.claimed_at == 1700000000 and card.claimed_until == 4102444800


def test_recreated_legacy_table_does_not_reuse_old_optional_columns(native_db):
    with native_db.cursor() as cur:
        cur.execute(
            "ALTER TABLE grep_462.tickets ADD COLUMN created_by text, "
            "ADD COLUMN claimed_by jsonb, ADD COLUMN claimed_at double precision, "
            "ADD COLUMN claimed_until double precision"
        )
    service = TicketService()
    assert (
        service.list_tickets(native_db, SCHEMA, TicketListFilters(limit=1)).total == 12
    )
    with native_db.cursor() as cur:
        cur.execute("DROP TABLE grep_462.tickets")
        cur.execute(get_tickets_table_sql(SCHEMA))
    result = service.list_tickets(native_db, SCHEMA, TicketListFilters(limit=1))
    assert result.total == 0 and result.tickets == []


def test_full_reader_remains_complete_without_card_projection(native_db):
    with native_db.cursor() as cur:
        cur.execute(
            "UPDATE grep_462.tickets SET description=%s WHERE id=1", ("x" * 100000,)
        )
    service = TicketService()
    result = service.list_tickets(
        native_db, SCHEMA, TicketListFilters(limit=1), fields="full"
    )
    assert result.tickets[0].description == "x" * 100000
    assert (
        service.get_ticket(
            native_db, SCHEMA, 1, include_history=False, include_links=False
        ).description
        == "x" * 100000
    )
