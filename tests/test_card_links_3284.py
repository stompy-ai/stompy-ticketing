"""Board cards carry compact links; the full board keeps the rows (STOMPY-3284).

After the board was bounded in SQL (0.8.24) a 70-card staging board still
answered ~296 KB: ticket links were 69 % of it and context links 17 %. Each
ticket-link row repeats source_title, target_title AND peer_title (plus both
statuses, kind, created_at), each context-link row repeats the card's own
title, and one hub card alone carried 87 KB of links.

fields="card" (MCP ticket_board, plugin REST board): a ticket link is
{peer_id, relation, direction, peer_status}, a context link is its label,
at most BOARD_CARD_LINK_CAP of each with links_total / context_links_total
when more exist. fields="full" (the host's web board) is byte-identical to
0.8.26: tests/fixtures/board_full_3284.json was written by origin/main.
"""

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

from stompy_ticketing.service import TicketService
from tests.board_fake import board_conn
from tests.test_service import FIXED_TIME, _make_ticket_row

SCHEMA = "test_project"
GOLDEN = Path(__file__).parent / "fixtures" / "board_full_3284.json"
CARD_LINK_KEYS = {"peer_id", "relation", "direction", "peer_status"}
_TYPES = ("related", "parent", "blocks", "duplicate")


def _title(i):
    return f"Ticket {i}: a realistic title about the thing this ticket tracks"


def _link(link_id, src, tgt, link_type, src_status="backlog", tgt_status="backlog"):
    return {
        "id": link_id, "source_id": src, "target_id": tgt, "link_type": link_type,
        "created_at": FIXED_TIME, "source_title": _title(src), "source_status": src_status,
        "target_title": _title(tgt), "target_status": tgt_status,
    }


def _ctx(link_id, ticket_id, label, link_type="references", version="latest"):
    return {
        "id": link_id, "ticket_id": ticket_id, "context_label": label,
        "context_version": version, "link_type": link_type, "created_at": FIXED_TIME,
        "ticket_title": _title(ticket_id), "ticket_status": "backlog",
    }


def small_fixture():
    """Three cards, every link shape once: the full-mode golden."""
    rows = [_make_ticket_row(id=i, title=_title(i)) for i in (1, 2, 3)]
    links = [
        _link(10, 1, 2, "blocks"),        # 1 blocks 2: 2 reads blocked_by
        _link(11, 3, 1, "parent"),        # incoming parent on 1
        _link(12, 1, 99, "related", tgt_status="done"),
        _link(13, 2, 3, "duplicate"),
    ]
    ctx = [_ctx(20, 1, "auth_design", "implements"), _ctx(21, 1, "api_spec"),
           _ctx(22, 2, "auth_design", "related", version="1.2")]
    return rows, {"ticket_links": links, "context_links": ctx}


def hub_fixture(cards=70, hub_links=200, hub_contexts=50):
    """A 70-card board whose card 1 is a hub with 200 ticket links."""
    rows = [_make_ticket_row(id=i, title=_title(i)) for i in range(1, cards + 1)]
    links, ctx, lid = [], [], 1
    for n in range(hub_links):
        peer = 1000 + n
        lt = _TYPES[n % len(_TYPES)]
        src, tgt = (1, peer) if n % 3 else (peer, 1)
        links.append(_link(lid, src, tgt, lt, tgt_status="done" if n % 5 == 0 else "backlog"))
        lid += 1
    for i in range(2, cards + 1):
        for k in range(3):
            links.append(_link(lid, i, 2000 + i * 3 + k, _TYPES[k]))
            lid += 1
        ctx += [_ctx(lid, i, f"context_{i}_a"), _ctx(lid + 1, i, f"context_{i}_b")]
        lid += 2
    ctx += [_ctx(lid + n, 1, f"hub_context_{n}") for n in range(hub_contexts)]
    return rows, {"ticket_links": links, "context_links": ctx}


def _board(fixture, fields):
    rows, link_rows = fixture
    conn, _ = board_conn(rows, link_rows=link_rows)
    svc = TicketService()
    svc.archive_stale_tickets = MagicMock(return_value=0)
    return svc.board_view(conn, SCHEMA, view="kanban", limit=0, fields=fields)


def _card(board, ticket_id):
    return next(t for c in board.columns for t in c.tickets if t.id == ticket_id)


def _host_bytes(board):
    """What the host web door sends (dementia-production routes/tickets.py)."""
    return json.dumps(board.model_dump(mode="json"), sort_keys=True).encode()


class TestCardLinksAreCompact:
    def test_card_ticket_link_is_peer_relation_direction_status(self):
        card = _card(_board(small_fixture(), "card"), 1)

        dumped = [link.model_dump() for link in card.links]
        assert all(set(d) == CARD_LINK_KEYS for d in dumped), dumped
        assert {(d["peer_id"], d["relation"], d["direction"]) for d in dumped} == {
            (2, "blocks", "outgoing"), (3, "parent", "incoming"), (99, "related", "outgoing"),
        }

    def test_card_link_keeps_the_viewer_relative_relation(self):
        card = _card(_board(small_fixture(), "card"), 2)

        dumped = {d["peer_id"]: d for d in (link.model_dump() for link in card.links)}
        assert dumped[1]["relation"] == "blocked_by" and dumped[1]["direction"] == "incoming"
        assert dumped[3] == {"peer_id": 3, "relation": "duplicate",
                             "direction": "outgoing", "peer_status": "backlog"}

    def test_card_context_links_are_labels(self):
        board = _board(small_fixture(), "card")

        assert _card(board, 1).context_links == ["auth_design", "api_spec"]
        assert _card(board, 2).context_links == ["auth_design"]

    def test_card_carries_no_title_of_any_link_end(self):
        text = json.dumps(_board(small_fixture(), "card").model_dump())

        for key in ("peer_title", "source_title", "target_title", "ticket_title"):
            assert key not in text, key

    def test_uncapped_card_carries_no_totals(self):
        dumped = _card(_board(small_fixture(), "card"), 1).model_dump()

        assert "links_total" not in dumped and "context_links_total" not in dumped


class TestHubCardIsCapped:
    def test_hub_card_ships_the_cap_and_names_the_total(self):
        card = _card(_board(hub_fixture(), "card"), 1)
        cap = TicketService.BOARD_CARD_LINK_CAP

        assert len(card.links) == cap and card.links_total == 200
        assert len(card.context_links) == cap and card.context_links_total == 50

    def test_cap_keeps_blockers_first_and_open_peers_before_closed(self):
        card = _card(_board(hub_fixture(), "card"), 1)

        relations = [link.relation for link in card.links]
        assert relations[0] == "blocked_by"
        order = ["blocked_by", "blocks", "parent", "duplicate", "related"]
        assert relations == sorted(relations, key=order.index)
        for rel in set(relations):
            statuses = [link.peer_status for link in card.links if link.relation == rel]
            assert statuses == sorted(statuses, key=lambda s: s == "done"), rel

    def test_hub_card_links_fit_in_two_kilobytes(self):
        card = _card(_board(hub_fixture(), "card"), 1)

        assert len(json.dumps([link.model_dump() for link in card.links])) < 2048


class TestFullBoardIsUnchanged:
    def test_full_board_is_byte_identical_to_0_8_26(self):
        assert _host_bytes(_board(small_fixture(), "full")) == GOLDEN.read_bytes().rstrip(b"\n")

    def test_full_board_keeps_every_link_row_with_titles(self):
        card = _card(_board(hub_fixture(), "full"), 1)

        assert len(card.links) == 200 and len(card.context_links) == 50
        assert card.links[0].peer_title and card.context_links[0].ticket_title


class TestDoorsShipCompactLinks:
    def test_mcp_ticket_board_text_has_no_link_titles(self):
        from stompy_ticketing.mcp_tools import register_ticketing_tools

        rows, link_rows = small_fixture()
        registered = {}
        mcp = MagicMock()
        mcp.tool = lambda: (lambda f: registered.setdefault(f.__name__, f))

        @contextmanager
        def db_ctx(project=None, require_write=True):
            yield board_conn(rows, link_rows=link_rows)[0]

        with patch.object(TicketService, "archive_stale_tickets", MagicMock(return_value=0)):
            register_ticketing_tools(
                mcp_instance=mcp, get_db_func=db_ctx,
                check_project_func=MagicMock(return_value=None),
                get_project_func=MagicMock(return_value="p"),
            )
            out = asyncio.run(registered["ticket_board"](project="p", limit=0))

        assert "auth_design" in out and "blocked_by" in out
        for key in ("peer_title", "source_title", "ticket_title", "Ticket 99:"):
            assert key not in out, key

    def test_plugin_rest_board_answers_compact_links(self):
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        from stompy_ticketing.api_routes import configure_routes, router

        rows, link_rows = small_fixture()

        @contextmanager
        def get_db(project=None, require_write=True):
            yield board_conn(rows, link_rows=link_rows)[0]

        async def go():
            configure_routes(get_db_func=get_db)
            app = FastAPI()
            app.include_router(router)
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                return await c.get("/projects/p/tickets/board")

        with patch.object(TicketService, "archive_stale_tickets", MagicMock(return_value=0)):
            r = asyncio.run(go())

        assert r.status_code == 200, r.text
        card = next(t for c in r.json()["columns"] for t in c["tickets"] if t["id"] == 1)
        assert all(set(link) == CARD_LINK_KEYS for link in card["links"]), card["links"]
        assert card["context_links"] == ["auth_design", "api_spec"]
        assert "links_total" not in card
