"""STOMPY-2455: a create names the open tickets it may duplicate.

Advisory only: the hint never blocks a create and its failure never fails
one. The similarity itself is PostgreSQL's (the stored content_tsvector), so
the real-text threshold evidence lives in the host's real-PG suite; these
tests pin the contract every door shares.
"""

import asyncio
import hashlib
import logging
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from stompy_ticketing import duplicates, mcp_tools
from stompy_ticketing.models import TicketCreate
from stompy_ticketing.service import TicketService, get_all_terminal_statuses
from tests.test_mcp_tools import _make_mock_mcp, _parse
from tests.test_service import FIXED_TIME, _make_ticket_row, _mock_conn_and_cursor, _sql_to_str


def _hit(id, similarity, title="Existing", status="triage"):
    return {"id": id, "title": title, "status": status, "similarity": similarity}


# --------------------------------------------------------------------------- #
# content_hash is read now: it is the exact-match fast path                   #
# --------------------------------------------------------------------------- #


def test_content_hash_is_the_one_formula_create_writes():
    expected = hashlib.sha256(b"Title|Body").hexdigest()[:16]
    assert duplicates.content_hash("Title", "Body") == expected
    assert duplicates.content_hash("Title", None) == duplicates.content_hash("Title", "")

    conn, cur = _mock_conn_and_cursor(fetchone_value=_make_ticket_row())
    with patch("stompy_ticketing.service.time") as t:
        t.time.return_value = FIXED_TIME
        TicketService().create_ticket(conn, "proj", TicketCreate(title="Title", description="Body"))
    assert expected in cur.execute.call_args.args[1]


def test_the_query_reads_content_hash_as_an_exact_match():
    conn, cur = _mock_conn_and_cursor(rows=[])
    duplicates.find_possible_duplicates(conn, "proj", "Title", "Body")
    query, params = cur.execute.call_args.args
    assert "content_hash = %(hash)s" in _sql_to_str(query)
    assert params["hash"] == duplicates.content_hash("Title", "Body")


# --------------------------------------------------------------------------- #
# The query: open tickets only, bounded, parameterized                        #
# --------------------------------------------------------------------------- #


def test_the_query_is_bounded_and_scoped_to_open_tickets():
    conn, cur = _mock_conn_and_cursor(rows=[])
    duplicates.find_possible_duplicates(conn, "my-proj", "Title", "Body", exclude_id=41)
    query, params = cur.execute.call_args.args
    text = _sql_to_str(query)
    assert "my-proj.tickets" in text
    assert "archived_at IS NULL" in text
    assert "NOT (status = ANY(%(terminal)s))" in text
    assert "LIMIT %(max_candidates)s" in text and "LIMIT %(pool)s" in text
    assert sorted(params["terminal"]) == sorted(get_all_terminal_statuses())
    assert params["exclude"] == 41
    assert params["max_candidates"] == duplicates.MAX_CANDIDATES
    assert params["pool"] == duplicates.TITLE_RERANK_POOL
    assert (params["title"], params["description"]) == ("Title", "Body")


def test_hints_below_the_threshold_are_dropped():
    rows = [_hit(9, 0.91), _hit(6, duplicates.SIMILARITY_THRESHOLD), _hit(5, 0.2), _hit(4, 0.1)]
    conn, _ = _mock_conn_and_cursor(rows=rows)
    hints = duplicates.find_possible_duplicates(conn, "proj", "Title", "Body")
    assert [h["id"] for h in hints] == [9, 6]


def test_hints_are_capped_best_first():
    rows = [_hit(i, 0.9 - i / 100) for i in range(1, 6)]
    conn, _ = _mock_conn_and_cursor(rows=rows)
    hints = duplicates.find_possible_duplicates(conn, "proj", "Title", "Body")
    assert [h["id"] for h in hints] == [1, 2, 3][: duplicates.MAX_HINTS]
    assert len(hints) == duplicates.MAX_HINTS


def test_a_hint_carries_display_id_title_status_and_a_rounded_similarity():
    conn, _ = _mock_conn_and_cursor(rows=[_hit(2451, 0.71802844, title="Should MCP…", status="open")])
    [hint] = duplicates.find_possible_duplicates(conn, "proj", "t", "d", prefix="stompy")
    assert hint == {
        "id": 2451,
        "display_id": "STOMPY-2451",
        "title": "Should MCP…",
        "status": "open",
        "similarity": 0.72,
    }
    conn, _ = _mock_conn_and_cursor(rows=[_hit(7, 0.5)])
    [hint] = duplicates.find_possible_duplicates(conn, "proj", "t", "d")
    assert hint["display_id"] == "7"


def test_nothing_similar_is_an_empty_list_not_a_failure():
    conn, _ = _mock_conn_and_cursor(rows=[_hit(3, 0.1)])
    assert duplicates.find_possible_duplicates(conn, "proj", "t", "d") == []
    conn.rollback.assert_not_called()


# --------------------------------------------------------------------------- #
# Fail soft, observably                                                       #
# --------------------------------------------------------------------------- #


def test_a_failing_check_returns_none_rolls_back_and_names_a_warning(caplog):
    conn, cur = _mock_conn_and_cursor()
    cur.execute.side_effect = RuntimeError("relation secret_schema.tickets missing")
    with caplog.at_level(logging.WARNING, logger="stompy_ticketing.duplicates"):
        assert duplicates.find_possible_duplicates(conn, "proj", "t", "d") is None
    conn.rollback.assert_called_once()
    [record] = [r for r in caplog.records if r.name == "stompy_ticketing.duplicates"]
    assert record.levelno == logging.WARNING
    assert record.getMessage() == "ticket_duplicate_hints_failed"
    assert record.error_type == "RuntimeError"
    assert "secret_schema" not in record.getMessage()


def test_a_failing_rollback_is_still_soft():
    conn, cur = _mock_conn_and_cursor()
    cur.execute.side_effect = RuntimeError("boom")
    conn.rollback.side_effect = RuntimeError("connection gone")
    assert duplicates.find_possible_duplicates(conn, "proj", "t", "d") is None


def test_response_fields_name_an_unavailable_check_instead_of_silence():
    assert duplicates.response_fields(None) == {"duplicate_check": "unavailable"}
    assert duplicates.response_fields([]) == {"possible_duplicates": []}
    hints = [{"id": 1, "display_id": "1", "title": "t", "status": "open", "similarity": 0.5}]
    assert duplicates.response_fields(hints) == {
        "possible_duplicates": hints,
        "duplicate_guidance": duplicates.GUIDANCE,
    }


# --------------------------------------------------------------------------- #
# The MCP door                                                                #
# --------------------------------------------------------------------------- #


@contextmanager
def _db(project=None, require_write=True):
    yield MagicMock()


def _ticket_tool():
    mcp = _make_mock_mcp()
    mcp_tools.register_ticketing_tools(
        mcp, _db, lambda p: None, lambda p: p or "proj",
        get_prefix_func=lambda p: "PROJ",
    )
    return mcp._registered_tools["ticket"]


def _created(id=12):
    from stompy_ticketing.ticket_projection import row_to_response

    return row_to_response({
        "id": id, "title": "New", "description": "Body", "type": "task",
        "status": "backlog", "priority": "medium", "created_at": FIXED_TIME,
        "updated_at": FIXED_TIME,
    })


def test_mcp_create_returns_the_hints_and_the_guidance(monkeypatch):
    find = MagicMock(return_value=[
        {"id": 5, "display_id": "PROJ-5", "title": "Old", "status": "triage", "similarity": 0.72}
    ])
    monkeypatch.setattr(duplicates, "find_possible_duplicates", find)
    monkeypatch.setattr(TicketService, "create_ticket", lambda *a, **k: _created(12))
    out = _parse(asyncio.run(_ticket_tool()(
        action="create", title="New", description="Body", project="proj",
    )))
    assert out["status"] == "created" and out["ticket"]["id"] == 12
    assert out["possible_duplicates"][0]["display_id"] == "PROJ-5"
    assert out["possible_duplicates"][0]["similarity"] == 0.72
    assert out["duplicate_guidance"] == duplicates.GUIDANCE
    args, kwargs = find.call_args
    assert args[2:4] == ("New", "Body")
    assert kwargs["exclude_id"] == 12 and kwargs["prefix"] == "PROJ"


def test_mcp_create_without_similar_tickets_says_nothing_extra(monkeypatch):
    monkeypatch.setattr(duplicates, "find_possible_duplicates", MagicMock(return_value=[]))
    monkeypatch.setattr(TicketService, "create_ticket", lambda *a, **k: _created(13))
    out = _parse(asyncio.run(_ticket_tool()(action="create", title="New", project="proj")))
    assert out["status"] == "created"
    assert "possible_duplicates" not in out and "duplicate_guidance" not in out


def test_mcp_create_survives_a_failed_check_and_says_so(monkeypatch):
    monkeypatch.setattr(duplicates, "find_possible_duplicates", MagicMock(return_value=None))
    monkeypatch.setattr(TicketService, "create_ticket", lambda *a, **k: _created(14))
    out = _parse(asyncio.run(_ticket_tool()(action="create", title="New", project="proj")))
    assert out["status"] == "created" and out["ticket"]["id"] == 14
    assert out["duplicate_check"] == "unavailable"


def test_mcp_create_dry_run_returns_candidates_and_writes_nothing(monkeypatch):
    find = MagicMock(return_value=[
        {"id": 5, "display_id": "PROJ-5", "title": "Old", "status": "triage", "similarity": 0.5}
    ])
    create = MagicMock()
    monkeypatch.setattr(duplicates, "find_possible_duplicates", find)
    monkeypatch.setattr(TicketService, "create_ticket", create)
    out = _parse(asyncio.run(_ticket_tool()(
        action="create", title="New", description="Body", project="proj", dry_run=True,
    )))
    create.assert_not_called()
    assert out["status"] == "dry_run"
    assert out["possible_duplicates"][0]["id"] == 5
    assert find.call_args.kwargs["exclude_id"] is None


def test_dry_run_is_refused_outside_create(monkeypatch):
    out = _parse(asyncio.run(_ticket_tool()(action="get", ticket_id=1, project="proj", dry_run=True)))
    assert "dry_run" in out["error"]
