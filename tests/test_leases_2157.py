"""Lease decisions with fixed clocks; real concurrency lives in host PG tests."""

from unittest.mock import patch

import pytest

from stompy_ticketing import leases
from tests.test_service import (FIXED_TIME, _make_ticket_row,
                                _mock_conn_and_cursor)


def row(**values):
    return {
        **_make_ticket_row(),
        "claimed_by": {"account_id": 51, "agent_label": "Astra"},
        "claimed_at": FIXED_TIME,
        "claimed_until": FIXED_TIME + 3600,
        **values,
    }


@pytest.mark.parametrize("actor", [None, "email@example.invalid", 0, -1, True])
def test_holder_requires_authenticated_positive_account(actor):
    with pytest.raises(ValueError):
        leases.holder_identity(actor, "Astra")


def test_holder_label_is_data_not_the_authenticated_account():
    assert leases.holder_identity("51", "Claude") == {
        "account_id": 51,
        "agent_label": "Claude",
    }
    with pytest.raises(ValueError):
        leases.holder_identity("51", "x" * 81)


@pytest.mark.parametrize("ttl", [0, 481, -1, 1.5, True])
def test_out_of_range_ttl_is_named_and_never_touches_database(ttl):
    conn, cur = _mock_conn_and_cursor()
    with pytest.raises(leases.LeaseRefused) as error:
        leases.claim(
            conn, "project", 1, leases.holder_identity(51, "Astra"), ttl, now=FIXED_TIME
        )
    assert error.value.code == "LEASE_TTL_OUT_OF_RANGE"
    cur.execute.assert_not_called()
    conn.commit.assert_not_called()


@pytest.mark.parametrize("age", [0, -1])
def test_expired_claim_is_hidden_without_mutating_stored_row(age):
    stored = row(claimed_until=FIXED_TIME + age)
    assert leases.live_claim_fields(stored, now=FIXED_TIME) == {}
    assert stored["claimed_by"]["agent_label"] == "Astra"


def test_live_claim_rendered_and_legacy_row_supported():
    stored = row()
    assert leases.live_claim_fields(stored, now=FIXED_TIME) == {
        key: stored[key] for key in ("claimed_by", "claimed_at", "claimed_until")
    }
    assert leases.live_claim_fields(_make_ticket_row(), now=FIXED_TIME) == {}


def test_held_refusal_names_actual_holder_and_does_not_commit():
    conn, cur = _mock_conn_and_cursor()
    cur.fetchone.side_effect = [None, row()]
    with pytest.raises(leases.LeaseRefused) as error:
        leases.claim(
            conn, "project", 1, leases.holder_identity(51, "Claude"), now=FIXED_TIME
        )
    assert error.value.code == "LEASE_HELD"
    assert error.value.payload()["claimed_by"] == row()["claimed_by"]
    assert error.value.payload()["claimed_until"] == FIXED_TIME + 3600
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()


def test_success_commits_and_returns_renewal_evidence():
    conn, cur = _mock_conn_and_cursor(fetchone_value=row(_renewing=True))
    ticket, via = leases.claim(
        conn, "project", 1, leases.holder_identity(51, "Astra"), now=FIXED_TIME
    )
    assert ticket["id"] == 1 and via == "renew"
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_claim_next_empty_is_explicit_and_commits_no_ticket():
    conn, cur = _mock_conn_and_cursor()
    assert (
        leases.claim_next(
            conn,
            "project",
            "Astra",
            leases.holder_identity(51, "Astra"),
            now=FIXED_TIME,
        )
        is None
    )
    conn.commit.assert_called_once()


def test_expired_release_is_noop_not_a_stale_holder_write():
    conn, cur = _mock_conn_and_cursor()
    cur.fetchone.side_effect = [None, row(claimed_until=FIXED_TIME)]
    ticket, changed = leases.release(
        conn, "project", 1, leases.holder_identity(51, "Claude"), now=FIXED_TIME
    )
    assert ticket["id"] == 1 and changed is False
    conn.commit.assert_called_once()


def test_row_projection_hides_expired_leases_without_losing_live_ones():
    from stompy_ticketing.service import TicketService

    with patch.object(leases, "_now", return_value=FIXED_TIME):
        service = TicketService()
        assert service._row_to_response(row()).claimed_by == row()["claimed_by"]
        expired = service._row_to_response(row(claimed_until=FIXED_TIME))
        assert expired.claimed_by is None and expired.claimed_until is None
