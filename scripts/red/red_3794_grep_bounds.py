"""Reuse the existing isolated plugin RED runner for bounds and cancellation."""

from pathlib import Path

import red_462_ticket_grep as runner

ROOT = Path(__file__).resolve().parents[2]
U = "tests/test_grep_bounds_3794.py::"
N = "tests/test_grep_timeout_postgres_3794.py::"
FILES = [U[:-2], N[:-2]]
CONTROLS = {
    U + "test_normal_glob_control_is_forwarded_unchanged",
    N + "test_native_normal_glob_control_preserves_page_and_totals",
}
LOCKS = {
    N + "test_real_lock_wait_is_cancelled_and_connection_is_reusable[" + d + "]"
    for d in ("service", "mcp")
}
REVERTS = [
    (
        "defer_glob_refusal_until_after_db",
        "stompy_ticketing/mcp_tools.py",
        'if action == "list" and grep is not None and len(grep) > MAX_GREP_LENGTH:',
        'if False and action == "list" and grep is not None and len(grep) > MAX_GREP_LENGTH:',
        {U + "test_10000_character_glob_refuses_before_database"},
    ),
    (
        "unbound_shared_glob",
        "stompy_ticketing/models.py",
        "max_length=MAX_GREP_LENGTH",
        "max_length=None",
        {U + "test_shared_filter_refuses_over_200_characters"},
    ),
    (
        "unbound_list_sql",
        "stompy_ticketing/ticket_listing.py",
        'LIST_STATEMENT_TIMEOUT = "5s"',
        'LIST_STATEMENT_TIMEOUT = "0"',
        LOCKS | {N + "test_real_list_bound_is_local_and_survives_maintenance_commit"},
    ),
    (
        "maintenance_discards_timeout",
        "stompy_ticketing/ticket_listing.py",
        "_bound_list(conn)\n        return _list_rows",
        "None\n        return _list_rows",
        {N + "test_real_list_bound_is_local_and_survives_maintenance_commit"},
    ),
    (
        "cancel_escapes_as_server_fault",
        "stompy_ticketing/ticket_listing.py",
        'raise ValueError(\n            "Ticket list timed out; narrow the filters and retry."\n        ) from exc',
        "raise exc",
        LOCKS | {U + "test_cancelled_list_rolls_back_and_refuses_as_value_error"},
    ),
]

if __name__ == "__main__":
    assert runner.ROOT == ROOT
    runner.FILES, runner.CONTROLS, runner.REVERTS = FILES, CONTROLS, REVERTS
    raise SystemExit(runner.main())
