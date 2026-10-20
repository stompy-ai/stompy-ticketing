"""Fields a stdlib log record carries reach the host's log (STOMPY-3089 follow-up).

The host formats stdlib records as "%(message)s": `extra=` fields never reach its log.
#51 moved db_error_answer's fields into the message; duplicates and lease_actions
still logged only through `extra`, so the schema / ticket a warning named was lost.
"""

import logging

from stompy_ticketing import duplicates, lease_actions
from tests.test_service import _mock_conn_and_cursor


def test_a_duplicate_check_failure_names_its_fields_in_the_message(caplog):
    conn, cur = _mock_conn_and_cursor()
    cur.execute.side_effect = RuntimeError("relation secret_schema.tickets missing")
    with caplog.at_level(logging.WARNING, logger="stompy_ticketing.duplicates"):
        duplicates.find_possible_duplicates(conn, "proj", "t", "d")
    [record] = [r for r in caplog.records if r.name == "stompy_ticketing.duplicates"]
    message = record.getMessage()
    assert "schema=proj" in message and "error_type=RuntimeError" in message
    assert "secret_schema" not in message


def test_a_lease_event_with_no_host_sink_names_its_fields_in_the_message(caplog, monkeypatch):
    monkeypatch.setattr(lease_actions, "_event", None)
    with caplog.at_level(logging.WARNING, logger="stompy_ticketing.lease_actions"):
        lease_actions._emit("warning", "ticket_lease_board_post_failed", ticket_id=7, project="p")
    [record] = [r for r in caplog.records if r.name == "stompy_ticketing.lease_actions"]
    message = record.getMessage()
    assert message.startswith("ticket_lease_board_post_failed")
    assert "ticket_id=7" in message and "project=p" in message
    assert record.ticket_id == 7  # extra kept for structured handlers
