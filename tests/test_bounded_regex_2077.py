"""The registered ticket_search handler must not stall sibling requests (2077)."""

import asyncio
import json
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from stompy_ticketing.mcp_tools import register_ticketing_tools
from stompy_ticketing.models import SearchResult, TicketResponse
from stompy_ticketing.safe_regex import compile_search_regex
from tests.tool_response import parse_tool_response

FIXED_TIME = 1700000000.0


@pytest.mark.parametrize("pattern", [None, 123, ["a"], b"a"])
def test_non_string_compiler_input_has_documented_refusal(pattern):
    with pytest.raises(ValueError, match="^Regex pattern must be a string$"):
        compile_search_regex(pattern)


def test_compiler_limits_and_unicode_contract():
    compiled = compile_search_regex("x" * 500)
    assert compiled.options.max_mem == 1024 * 1024
    assert compiled.options.log_errors is False
    assert compile_search_regex("café").search("CAFÉ")
    assert compile_search_regex(r"^\w+$").search("café") is None
    assert compile_search_regex(r"^\p{L}+$").search("café")
    with pytest.raises(ValueError):
        compile_search_regex("\ud800")


def _registered_search(tickets):
    registered = {}
    mcp = Mock()

    def decorator(fn):
        registered[fn.__name__] = fn
        return fn

    mcp.tool.return_value = decorator
    db = Mock(return_value=nullcontext(Mock()))
    service = Mock()
    service.search_tickets.return_value = SearchResult(
        tickets=tickets, total=len(tickets), query="test"
    )
    with patch("stompy_ticketing.mcp_tools.TicketService", return_value=service):
        register_ticketing_tools(mcp, db, lambda p: None, lambda p: "fixture")
    return registered["ticket_search"], db, service


def _ticket(id, title="ordinary", description="body"):
    return TicketResponse(
        id=id,
        title=title,
        description=description,
        type="task",
        status="backlog",
        priority="medium",
        created_at=FIXED_TIME,
        updated_at=FIXED_TIME,
    )


@pytest.mark.parametrize("pattern", ["[", r"(?=body)", r"(body)\1", "x" * 501])
def test_invalid_or_unsupported_regex_refused_before_database(pattern):
    search, db, _ = _registered_search([])
    with patch("stompy_ticketing.mcp_tools._toon_encode", side_effect=json.dumps):
        payload = parse_tool_response(asyncio.run(search(query="test", project="fixture", regex=pattern)))
    assert "error" in payload
    db.assert_not_called()


@pytest.mark.parametrize("fields", ["card", "full"])
def test_registered_search_matches_title_or_body_and_keeps_projection(fields):
    search, _, service = _registered_search(
        [
            _ticket(1, "FIX API"),
            _ticket(2, description="fix api endpoint"),
            _ticket(3),
        ]
    )
    with patch("stompy_ticketing.mcp_tools._toon_encode", side_effect=json.dumps):
        payload = parse_tool_response(
            asyncio.run(
                search(
                    query="test",
                    project="fixture",
                    regex="fix.*api",
                    fields=fields,
                    limit=2,
                )
            )
        )
    assert [t["id"] for t in payload["tickets"]] == [1, 2]
    assert payload["total"] == 2
    assert ("description" in payload["tickets"][0]) is (fields == "full")
    assert service.search_tickets.call_args.kwargs["fields"] == "full"
    assert service.search_tickets.call_args.kwargs["limit"] == 6


@pytest.mark.parametrize("carrier", ["title", "description"])
def test_adversarial_search_keeps_event_loop_responsive(carrier):
    # A separate OS process is the TEST watchdog, never a production timeout.
    # The legacy matcher can hold the GIL, making an in-loop timeout ineffective.
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), carrier],
            capture_output=True,
            text=True,
            timeout=12,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("ticket_search exceeded external watchdog; regex work did not finish")
    assert result.returncode == 0, result.stdout + result.stderr


async def _adversarial(carrier):
    search, _, _ = _registered_search([_ticket(1, **{carrier: "a" * 26 + "!"})])
    loop = asyncio.get_running_loop()
    ready = asyncio.Event()
    delays = []

    async def heartbeat():
        start = loop.time()
        ready.set()
        await asyncio.sleep(0.01)
        delays.append(loop.time() - start)

    sibling = asyncio.create_task(heartbeat())
    await ready.wait()
    with patch("stompy_ticketing.mcp_tools._toon_encode", side_effect=json.dumps):
        payload = parse_tool_response(await search(query="test", project="fixture", regex=r"(a+)+$"))
    await sibling
    assert "error" not in payload, payload
    assert payload["total"] == 0
    assert max(delays) < 0.5, f"Sibling request stalled for {max(delays):.3f}s"


if __name__ == "__main__":
    asyncio.run(_adversarial(sys.argv[1]))
