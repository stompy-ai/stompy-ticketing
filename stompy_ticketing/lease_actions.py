"""Shared lease action outcome and post-commit, fail-soft host effects."""

import logging

from stompy_ticketing import leases
from stompy_ticketing.ticket_projection import row_to_response

logger = logging.getLogger(__name__)
_board_post = None
_event = None
_actor = None


def configure(*, board_post=None, event=None, actor=None):
    global _board_post, _event, _actor
    _board_post, _event, _actor = board_post, event, actor


def _emit(level, event, **fields):
    if _event:
        _event(level, event, **fields)
    else:
        getattr(logger, level)(event, extra=fields)


def run_action(
    conn,
    schema,
    project,
    action,
    actor,
    agent_label="",
    ticket_id=None,
    ttl_minutes=60,
    assignee=None,
):
    holder = leases.holder_identity(actor, agent_label)
    try:
        if action == "claim":
            row, via = leases.claim(conn, schema, ticket_id, holder, ttl_minutes)
            changed = True
        elif action == "claim_next":
            row = leases.claim_next(conn, schema, assignee, holder, ttl_minutes)
            if row is None:
                return {"status": "empty", "ticket": None}
            via, changed = "claim_next", True
        elif action == "release":
            row, changed = leases.release(conn, schema, ticket_id, holder)
            via = "release"
        else:
            raise ValueError("Unknown lease action")
    except leases.LeaseRefused as exc:
        _emit(
            "warning",
            "ticket_claim_refused",
            project=project,
            ticket=ticket_id,
            reason=exc.reason,
        )
        raise
    # The core has committed already. Board failure cannot undo ticket ownership.
    if action == "release":
        _emit(
            "info",
            "ticket_released",
            project=project,
            ticket=row["id"],
            holder=holder,
            changed=changed,
        )
    else:
        _emit(
            "info",
            "ticket_claimed",
            project=project,
            ticket=row["id"],
            holder=holder,
            until=row["claimed_until"],
            via=via,
        )
    if changed:
        try:
            if _board_post is None:
                raise RuntimeError("No host board integration configured")
            _board_post(
                project=project,
                schema=schema,
                holder=holder,
                ticket=row,
                via=via,
                ttl_minutes=ttl_minutes,
            )
        except Exception as exc:
            _emit(
                "warning",
                "ticket_claim_board_post_failed",
                project=project,
                ticket=row["id"],
                error_type=type(exc).__name__,
            )
    from stompy_ticketing.service import TicketService

    ticket = TicketService.to_card(row_to_response(row)).model_dump()
    return {
        "status": "released" if action == "release" else "claimed",
        "changed": changed,
        "via": via,
        "ticket": ticket,
    }
