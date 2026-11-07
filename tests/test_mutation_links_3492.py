"""STOMPY-3492 item 1: a REST mutation answers the ticket's links, not `links: []`.

Dogfood 6.6.35 round 4: POST /tickets/5/move answered the full ticket shape with
`"links": []` and `"context_links": []` for a ticket that HAS a context link, while
GET of the same ticket listed it. A full-looking record whose empty arrays read
"this ticket has no links" is a wrong answer, so the REST doors fill them the way
GET does (`TicketService.read_links`), read BEFORE the write so a failed read
refuses with nothing written. The service's own transition/close keep skipping
links: they run once per step inside close walks and batch moves.
"""

from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from stompy_ticketing.models import ContextLinkResponse, TicketLinkResponse
from tests.test_api import FIXED_TIME, _create_test_app, _make_mock_conn, _make_ticket_row

LINK = TicketLinkResponse(id=2, source_id=5, target_id=6, link_type="blocks")
CTX = ContextLinkResponse(
    id=2, ticket_id=5, context_label="wine_and_refreshments", context_version="1.0",
    link_type="updates",
)


def _patched():
    return (
        patch("stompy_ticketing.service.TicketService._get_links_for_ticket", return_value=[LINK]),
        patch("stompy_ticketing.service.context_links_present", return_value=True),
        patch(
            "stompy_ticketing.service.TicketService.list_context_links_for_ticket",
            return_value=[CTX],
        ),
        patch("stompy_ticketing.service.time"),
    )


async def _call(method, path, body, rows):
    conn, cur = _make_mock_conn()
    cur.fetchone.side_effect = rows
    cur.fetchall.return_value = []
    app = _create_test_app(conn, cur)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, json=body)


@pytest.mark.asyncio
async def test_move_answers_the_tickets_links():
    a, b, c, t = _patched()
    with a, b, c, t as mock_time:
        mock_time.time.return_value = FIXED_TIME
        r = await _call(
            "POST",
            "/projects/p/tickets/5/move",
            {"status": "in_progress"},
            [_make_ticket_row(id=5), _make_ticket_row(id=5, status="in_progress")],
        )
    assert r.status_code == 200, r.text
    assert [x["id"] for x in r.json()["links"]] == [2]
    assert [x["context_label"] for x in r.json()["context_links"]] == ["wine_and_refreshments"]


@pytest.mark.asyncio
async def test_update_answers_the_tickets_links():
    a, b, c, t = _patched()
    with a, b, c, t as mock_time:
        mock_time.time.return_value = FIXED_TIME
        r = await _call(
            "PUT",
            "/projects/p/tickets/5",
            {"priority": "high"},
            [_make_ticket_row(id=5), _make_ticket_row(id=5, priority="high")],
        )
    assert r.status_code == 200, r.text
    assert [x["id"] for x in r.json()["links"]] == [2]
    assert len(r.json()["context_links"]) == 1


def test_get_ticket_and_the_doors_are_one_read():
    """get_ticket fills links through read_links, so GET and a mutation cannot drift."""
    import inspect

    from stompy_ticketing.service import TicketService

    assert "self.read_links(" in inspect.getsource(TicketService.get_ticket)


@pytest.mark.asyncio
async def test_a_failed_links_read_writes_nothing():
    with patch(
        "stompy_ticketing.service.TicketService._get_links_for_ticket",
        side_effect=RuntimeError("links table unreadable"),
    ), patch("stompy_ticketing.service.TicketService.transition_ticket") as move:
        with pytest.raises(RuntimeError):
            await _call("POST", "/projects/p/tickets/5/move", {"status": "in_progress"}, [])
    move.assert_not_called()
