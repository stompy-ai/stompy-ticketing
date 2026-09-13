"""Fixed-clock archival decisions; host fixtures prove actual SQL and admission."""

from unittest.mock import patch

import pytest

from stompy_ticketing.service import TicketService
from tests.test_service import (
    FIXED_TIME,
    _make_ticket_row,
    _mock_conn_and_cursor as _base_cursor,
)


def _mock_conn_and_cursor(**kwargs):
    conn, cur = _base_cursor(**kwargs)
    cur.__enter__.return_value = cur
    return conn, cur


@pytest.mark.parametrize("status", ["parked", "done", "cancelled"])
def test_archive_changes_only_target_and_preserves_status(status):
    before = _make_ticket_row(status=status, archived_at=None)
    after = {**before, "archived_at": FIXED_TIME, "updated_at": FIXED_TIME}
    conn, cur = _mock_conn_and_cursor()
    cur.fetchone.side_effect = [before, after]
    with patch("time.time", return_value=FIXED_TIME):
        result = TicketService().archive_ticket(conn, "project", 1, changed_by="51")
    assert result.archived_at == FIXED_TIME and result.status == status
    conn.commit.assert_called_once()
    statements = [str(call.args[0]) for call in cur.execute.call_args_list]
    assert "FOR UPDATE" in statements[0]
    assert "WHERE id = %s" in statements[1]
    assert "ticket_history" in statements[2]


def test_live_nonterminal_archive_is_named_refusal_and_does_not_write():
    from stompy_ticketing.archival import ArchiveRefused

    conn, cur = _mock_conn_and_cursor(fetchone_value=_make_ticket_row(status="backlog"))
    with pytest.raises(ArchiveRefused) as error:
        TicketService().archive_ticket(conn, "project", 1, changed_by="51")
    assert error.value.code == "ARCHIVE_NOT_ALLOWED"
    assert len(cur.execute.call_args_list) == 1
    conn.commit.assert_not_called()
    conn.rollback.assert_called_once()


@pytest.mark.parametrize(
    "restoring,archived,reason",
    [(False, FIXED_TIME, "already_archived"), (True, None, "not_archived")],
)
def test_repeated_archive_or_restore_is_named_refusal_without_second_history_write(
    restoring, archived, reason
):
    from stompy_ticketing.archival import ArchiveRefused

    conn, cur = _mock_conn_and_cursor(
        fetchone_value=_make_ticket_row(archived_at=archived)
    )
    method = (
        TicketService().unarchive_ticket
        if restoring
        else TicketService().archive_ticket
    )
    with pytest.raises(ArchiveRefused) as error:
        method(conn, "project", 1, changed_by="51")
    assert error.value.reason == reason
    assert len(cur.execute.call_args_list) == 1
    conn.commit.assert_not_called()


def test_unarchive_preserves_status_and_clears_only_archival_marker():
    before = _make_ticket_row(status="done", archived_at=FIXED_TIME - 1)
    after = {**before, "archived_at": None, "updated_at": FIXED_TIME}
    conn, cur = _mock_conn_and_cursor()
    cur.fetchone.side_effect = [before, after]
    with patch("time.time", return_value=FIXED_TIME):
        result = TicketService().unarchive_ticket(conn, "project", 1, changed_by="51")
    assert result.archived_at is None and result.status == "done"
    assert cur.execute.call_args_list[1].args[1][0] is None
    conn.commit.assert_called_once()


def test_batch_archive_preview_does_not_commit_or_update():
    conn, cur = _mock_conn_and_cursor()
    cur.fetchall.return_value = [_make_ticket_row(status="parked")]
    result = TicketService().batch_archive(conn, "project", [1], confirm=False)
    assert result.dry_run and result.succeeded == 1
    assert result.results[0].ticket_id == 1
    assert len(cur.execute.call_args_list) == 1
    conn.commit.assert_not_called()
