"""Agent-facing receipts keep useful state, without echoing the write body."""

from unittest.mock import MagicMock, patch

from toon import decode

from stompy_ticketing.duplicates import GUIDANCE, response_fields
from stompy_ticketing.errors import recoverable_error
from stompy_ticketing.models import SearchResult, TicketResponse
from stompy_ticketing.service import InvalidTransitionError, validate_transition
from tests.test_cluster_c_fixes import _register, _run

BODY = "write content " * 1000
TOKEN = 1795084338.6985672


def answer(action, metadata=None):
    service = MagicMock()
    ticket = TicketResponse(
        id=7,
        title="fixture",
        description=BODY,
        type="task",
        status="backlog",
        priority="medium",
        created_by="51",
        created_at=TOKEN,
        updated_at=TOKEN,
        metadata=metadata,
    )
    service.create_ticket.return_value = ticket
    service.update_ticket.return_value = ticket
    tools, _ = _register(mock_svc=service)
    args = {"title": "fixture", "description": BODY}
    if action == "update":
        args["ticket_id"] = 7
    with patch("stompy_ticketing.duplicates.find_possible_duplicates", return_value=[]):
        return decode(_run(tools["ticket"](action=action, **args)))


def test_create_receipt_is_a_card_with_size_and_action():
    result = answer("create")
    assert result["action"] == "created" and "status" not in result
    assert "description" not in result["ticket"]
    assert result["ticket"]["description_chars"] == len(BODY)
    assert result["ticket"]["description_preview"] == BODY[:100] + "..."
    assert "created_by" not in result["ticket"]


def test_update_receipt_is_a_card_with_size_and_action():
    result = answer("update")
    assert result["action"] == "updated" and "description" not in result["ticket"]
    assert result["ticket"]["description_chars"] == len(BODY)


def test_readable_time_does_not_round_the_concurrency_token():
    result = answer("create")["ticket"]
    assert result["created_at_iso"] == "2026-11-19T10:32:18Z"
    assert result["updated_at_iso"] == "2026-11-19T10:32:18Z"
    assert result["updated_at"] == TOKEN


def test_numeric_token_remains_exact_independently_of_display():
    assert answer("create")["ticket"]["updated_at"] == TOKEN
    assert answer("update")["ticket"]["updated_at"] == TOKEN


def test_user_metadata_is_not_rewritten_as_a_card():
    metadata = {
        "id": 99,
        "title": "stored record",
        "status": "backlog",
        "type": "task",
        "priority": "low",
        "created_by": "historicactor",
        "description_chars": 3,
        "updated_at": 1.23,
    }
    assert answer("create", metadata=metadata)["ticket"]["metadata"] == metadata


def test_missing_title_uses_house_refusal_shape():
    tools, _ = _register(mock_svc=MagicMock())
    result = decode(_run(tools["ticket"](action="create")))
    assert result["success"] is False and result["error"] == "TITLE_REQUIRED"
    assert "message" in result


def test_invalid_transition_carries_allowed_next_as_data():
    try:
        validate_transition("bug", "triage", "resolved")
    except InvalidTransitionError as exc:
        refusal = exc
    service = MagicMock()
    service.transition_ticket.side_effect = refusal
    tools, _ = _register(mock_svc=service)
    result = decode(
        _run(tools["ticket"](action="move", ticket_id=7, status="resolved"))
    )
    assert result["success"] is False and result["error"] == "INVALID_TRANSITION"
    assert result["allowed_next"] == ["confirmed", "wont_fix", "parked"]
    assert "close" in result["next"]


def test_close_refusal_names_terminal_choices_instead_of_repeating_close():
    from stompy_ticketing.service import TicketService

    try:
        TicketService()._split_resolution("resolved", "task", ["done", "cancelled"])
    except InvalidTransitionError as exc:
        refusal = exc
    service = MagicMock()
    service.close_ticket.side_effect = refusal
    tools, _ = _register(mock_svc=service)
    result = decode(
        _run(tools["ticket"](action="close", ticket_id=7, resolution="resolved"))
    )
    assert result["allowed_next"] == ["done", "cancelled"]
    assert "resolution" in result["next"]
    assert "action=close" not in result["next"]


def test_weak_duplicate_hint_is_advisory_without_close_instruction():
    hints = [{"id": 8, "similarity": 0.36}]
    result = response_fields(hints)
    assert result["possible_duplicates"] is hints
    assert "similar" in result["duplicate_guidance"].lower()
    assert "close" not in result["duplicate_guidance"]


def test_strong_duplicate_guidance_and_candidates_are_preserved():
    hints = [{"id": 8, "similarity": 0.72}]
    result = response_fields(hints)
    assert result["possible_duplicates"] is hints
    assert result["duplicate_guidance"] == GUIDANCE


def test_search_keeps_zero_result_but_omits_argument_echoes():
    service = MagicMock()
    service.search_tickets.return_value = SearchResult(
        tickets=[], total=0, query="fixture"
    )
    tools, _ = _register(mock_svc=service)
    result = decode(_run(tools["ticket_search"](query="fixture")))
    assert result["total"] == 0
    assert "query" not in result and "include_archived" not in result


def test_refusals_use_the_same_default_toon_format_as_reads():
    tools, _ = _register(mock_svc=MagicMock())
    raw = _run(tools["ticket"](action="create"))
    assert raw.startswith("success: false\n")
    assert decode(raw)["error"] == "TITLE_REQUIRED"


def test_refusal_recovery_steps_survive_codec_roundtrip_failure():
    import json

    steps = ["Pass a reason", "revisit_by, if given, is an ISO date: YYYY-MM-DD"]
    raw = recoverable_error("PARK_ARGUMENT", "parking requires a reason", steps)
    parsed = json.loads(raw) if raw.startswith("{") else decode(raw)
    assert parsed["recovery"]["steps"] == steps
    assert parsed["success"] is False and parsed["error"] == "PARK_ARGUMENT"
