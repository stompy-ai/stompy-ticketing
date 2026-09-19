"""STOMPY-2380: the REST door records WHO wrote, never what they typed.

`POST /batch/move` and `/batch/close` stored the body's free-text `note` as
`ticket_history.changed_by`, so any caller could write history as any user id
(and the MCP door then resolved that id to the user's display name). `PUT
/{id}` and `POST /{id}/move` recorded no actor at all. Every REST write now
stamps the host's `actor_func()` — the same identity the MCP door records.

The service is a spy: these tests pin what the ROUTE hands it. The history
row it produces from `changed_by` is covered by the service tests, and the
host proves the whole chain on real PostgreSQL through its real middleware.
"""

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from stompy_ticketing import api_routes, plugin
from stompy_ticketing.models import BatchOperationResult, TicketResponse

CALLER = "5692"


def _ticket(**kw):
    return TicketResponse(**{"id": 1, "title": "t", "type": "task", "status": "backlog",
                             "priority": "medium", **kw})


@contextmanager
def _db(project=None, require_write=True):
    yield MagicMock()


@pytest.fixture
def door(monkeypatch):
    """(client, service spy, configure) — configure(actor_func) rebinds the
    REST globals exactly as the host's registration does."""
    service = MagicMock()
    service.batch_transition.return_value = BatchOperationResult(
        action="move", total=1, succeeded=1, failed=0, dry_run=False)
    service.batch_close.return_value = BatchOperationResult(
        action="close", total=1, succeeded=1, failed=0, dry_run=False)
    service.update_ticket.return_value = _ticket()
    service.transition_ticket.return_value = _ticket(status="in_progress")
    monkeypatch.setattr(api_routes, "_service", service)

    def configure(actor_func):
        api_routes.configure_routes(get_db_func=_db, actor_func=actor_func)

    app = FastAPI()
    app.include_router(api_routes.router)
    yield TestClient(app), service, configure
    api_routes.configure_routes(get_db_func=_db)


# ---------------------------------------------------------------- the forgery


@pytest.mark.parametrize("forged", ["51", "6746"])
def test_batch_move_note_naming_another_user_is_not_the_actor(door, forged):
    client, service, configure = door
    configure(lambda: CALLER)
    r = client.post("/projects/p/tickets/batch/move",
                    json={"ticket_ids": [1], "status": "in_progress", "confirm": True,
                          "note": forged})
    assert r.status_code == 200, r.text
    assert service.batch_transition.call_args.kwargs["changed_by"] == CALLER


@pytest.mark.parametrize("forged", ["51", "6746"])
def test_batch_close_note_naming_another_user_is_not_the_actor(door, forged):
    client, service, configure = door
    configure(lambda: CALLER)
    r = client.post("/projects/p/tickets/batch/close",
                    json={"ticket_ids": [1], "confirm": True, "note": forged})
    assert r.status_code == 200, r.text
    assert service.batch_close.call_args.kwargs["changed_by"] == CALLER


def test_without_an_actor_hook_a_note_still_never_becomes_the_actor(door):
    """An older host that wires no actor gets NULL, as the MCP door does —
    never the caller's text."""
    client, service, configure = door
    configure(None)
    client.post("/projects/p/tickets/batch/move",
                json={"ticket_ids": [1], "status": "done", "confirm": True, "note": "51"})
    client.post("/projects/p/tickets/batch/close",
                json={"ticket_ids": [1], "confirm": True, "note": "51"})
    assert service.batch_transition.call_args.kwargs["changed_by"] is None
    assert service.batch_close.call_args.kwargs["changed_by"] is None


# ------------------------------------------------ routes that recorded nobody


def test_put_update_records_the_caller(door):
    client, service, configure = door
    configure(lambda: CALLER)
    r = client.put("/projects/p/tickets/1", json={"title": "renamed"})
    assert r.status_code == 200, r.text
    assert service.update_ticket.call_args.kwargs["changed_by"] == CALLER


def test_single_move_records_the_caller(door):
    client, service, configure = door
    configure(lambda: CALLER)
    r = client.post("/projects/p/tickets/1/move", json={"status": "in_progress"})
    assert r.status_code == 200, r.text
    assert service.transition_ticket.call_args.kwargs["changed_by"] == CALLER


def test_a_raising_actor_hook_never_fails_the_write(door):
    """Identity is metadata: same contract as the MCP door's `_actor()`."""
    client, service, configure = door

    def boom():
        raise RuntimeError("identity unavailable")

    configure(boom)
    r = client.post("/projects/p/tickets/batch/move",
                    json={"ticket_ids": [1], "status": "in_progress", "confirm": True})
    assert r.status_code == 200, r.text
    assert service.batch_transition.call_args.kwargs["changed_by"] is None


def test_register_plugin_hands_the_actor_hook_to_the_rest_door(door):
    """The one registration call wires BOTH doors from one `actor_func`."""
    client, service, _ = door
    plugin.register_plugin(
        mcp_instance=MagicMock(), api_router=MagicMock(), get_db_func=_db,
        check_project_func=MagicMock(return_value=None),
        get_project_func=MagicMock(return_value="p"),
        actor_func=lambda: CALLER,
    )
    client.post("/projects/p/tickets/batch/move",
                json={"ticket_ids": [1], "status": "done", "confirm": True, "note": "51"})
    assert service.batch_transition.call_args.kwargs["changed_by"] == CALLER


# ------------------------------------------------------------------ controls


def test_control_batch_move_still_forwards_its_other_fields(door):
    """GREEN under every revert: the fix changes the actor and nothing else."""
    client, service, configure = door
    configure(lambda: CALLER)
    client.post("/projects/p/tickets/batch/move",
                json={"ticket_ids": [1, 2], "status": "parked", "confirm": True,
                      "reason": "later", "revisit_by": "2026-12-01", "note": "51"})
    args, kwargs = service.batch_transition.call_args
    assert args[2:] == ([1, 2], "parked")
    assert (kwargs["confirm"], kwargs["reason"], kwargs["revisit_by"]) == (
        True, "later", "2026-12-01")


def test_control_single_move_still_forwards_park_reason(door):
    client, service, configure = door
    configure(lambda: CALLER)
    client.post("/projects/p/tickets/1/move",
                json={"status": "parked", "reason": "later", "revisit_by": "2026-12-01"})
    args, kwargs = service.transition_ticket.call_args
    assert args[2:] == (1, "parked")
    assert (kwargs["reason"], kwargs["revisit_by"]) == ("later", "2026-12-01")
