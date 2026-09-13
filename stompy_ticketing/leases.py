"""Atomic expiring ticket ownership; no sweeper, status move, or unit creation."""

import time

from psycopg2 import sql
from psycopg2.extras import Json

NEXT_STATUSES = ("backlog", "confirmed", "approved", "open")
CLAIM_STATUSES = NEXT_STATUSES + ("in_progress",)


def _now():
    return time.time()


class LeaseRefused(ValueError):
    def __init__(self, code, reason, message, row=None):
        super().__init__(message)
        self.code, self.reason, self.row = code, reason, row

    def payload(self):
        result = {"error": self.code, "message": str(self)}
        if self.row and self.code == "LEASE_HELD":
            result.update(
                claimed_by=self.row["claimed_by"],
                claimed_until=self.row["claimed_until"],
            )
        return result


def holder_identity(actor, agent_label=""):
    # The host supplies actor; labels never confer authority or select accounts.
    if isinstance(actor, bool) or not str(actor).isdigit() or int(actor) <= 0:
        raise ValueError("An authenticated positive account ID is required for a lease")
    if not isinstance(agent_label, str) or len(agent_label) > 80:
        raise ValueError("agent_label must be text of at most 80 characters")
    return {"account_id": int(actor), "agent_label": agent_label}


def _ttl(ttl_minutes):
    if type(ttl_minutes) is not int or not 1 <= ttl_minutes <= 480:
        raise LeaseRefused(
            "LEASE_TTL_OUT_OF_RANGE",
            "ttl_out_of_range",
            "ttl_minutes must be an integer from 1 to 480",
        )
    return ttl_minutes * 60


def live_claim_fields(row, *, now=None):
    now = _now() if now is None else now
    if not row.get("claimed_by") or (row.get("claimed_until") or 0) <= now:
        return {}
    return {key: row.get(key) for key in ("claimed_by", "claimed_at", "claimed_until")}


def _row(cur, schema, ticket_id):
    cur.execute(
        sql.SQL("SELECT * FROM {}.tickets WHERE id=%s").format(sql.Identifier(schema)),
        (ticket_id,),
    )
    return cur.fetchone()


def _refuse(row, now):
    if not row:
        raise LeaseRefused("LEASE_NOT_FOUND", "not_found", "Ticket not found")
    if row["status"] not in CLAIM_STATUSES or row.get("archived_at") is not None:
        raise LeaseRefused(
            "LEASE_NOT_CLAIMABLE",
            "not_claimable_status",
            "Ticket is not in a claimable work status",
        )
    if live_claim_fields(row, now=now):
        raise LeaseRefused(
            "LEASE_HELD", "held", "Ticket has a live lease held by another agent", row
        )
    raise LeaseRefused(
        "LEASE_NOT_CLAIMABLE",
        "not_claimable_status",
        "Ticket changed during claim; re-read and retry",
    )


def claim(conn, schema, ticket_id, holder, ttl_minutes=60, *, now=None):
    duration = _ttl(ttl_minutes)
    now = _now() if now is None else now
    if not ticket_id:
        raise LeaseRefused("LEASE_NOT_FOUND", "not_found", "ticket_id is required")
    cur = conn.cursor()
    try:
        # One atomic statement. The locked old row also tells the caller whether
        # this was renewal, including two operations with the same injected clock.
        cur.execute(
            sql.SQL("""
            WITH previous AS (
                SELECT id, claimed_by=%s::jsonb AND claimed_until>%s AS renewing
                FROM {s}.tickets WHERE id=%s FOR UPDATE
            )
            UPDATE {s}.tickets AS t SET
                claimed_by=%s::jsonb,
                claimed_at=CASE WHEN previous.renewing THEN t.claimed_at ELSE %s END,
                claimed_until=CASE WHEN previous.renewing
                    THEN GREATEST(t.claimed_until+0.000001,%s) ELSE %s END,
                updated_at=GREATEST(COALESCE(t.updated_at,0)+0.000001,%s)
            FROM previous WHERE t.id=previous.id AND t.archived_at IS NULL
                AND t.status=ANY(%s)
                AND (t.claimed_until IS NULL OR t.claimed_until<=%s OR t.claimed_by=%s::jsonb)
            RETURNING t.*, previous.renewing AS _renewing
        """).format(s=sql.Identifier(schema)),
            (
                Json(holder),
                now,
                ticket_id,
                Json(holder),
                now,
                now + duration,
                now + duration,
                now,
                list(CLAIM_STATUSES),
                now,
                Json(holder),
            ),
        )
        row = cur.fetchone()
        if row is None:
            _refuse(_row(cur, schema, ticket_id), now)
        via = "renew" if row.pop("_renewing", False) else "claim"
        conn.commit()
        return row, via
    except BaseException:
        conn.rollback()
        raise


def claim_next(conn, schema, assignee, holder, ttl_minutes=60, *, now=None):
    duration = _ttl(ttl_minutes)
    now = _now() if now is None else now
    if not isinstance(assignee, str) or not assignee.strip():
        raise ValueError("assignee is required for claim_next")
    cur = conn.cursor()
    try:
        cur.execute(
            sql.SQL("""
            WITH candidate AS (
                SELECT id FROM {s}.tickets
                WHERE assignee=%s AND status=ANY(%s) AND archived_at IS NULL
                    AND (claimed_until IS NULL OR claimed_until<=%s)
                ORDER BY CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                    WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END,
                    created_at ASC NULLS LAST, id ASC
                LIMIT 1 FOR UPDATE SKIP LOCKED
            )
            UPDATE {s}.tickets AS t SET claimed_by=%s::jsonb, claimed_at=%s,
                claimed_until=%s, updated_at=GREATEST(COALESCE(t.updated_at,0)+0.000001,%s)
            FROM candidate WHERE t.id=candidate.id RETURNING t.*
        """).format(s=sql.Identifier(schema)),
            (
                assignee,
                list(NEXT_STATUSES),
                now,
                Json(holder),
                now,
                now + duration,
                now,
            ),
        )
        row = cur.fetchone()
        conn.commit()
        return row
    except BaseException:
        conn.rollback()
        raise


def release(conn, schema, ticket_id, holder, *, now=None):
    now = _now() if now is None else now
    cur = conn.cursor()
    try:
        cur.execute(
            sql.SQL("""
            UPDATE {}.tickets SET claimed_by=NULL, claimed_at=NULL, claimed_until=NULL,
                updated_at=GREATEST(COALESCE(updated_at,0)+0.000001,%s)
            WHERE id=%s AND claimed_by=%s::jsonb AND claimed_until>%s RETURNING *
        """).format(sql.Identifier(schema)),
            (now, ticket_id, Json(holder), now),
        )
        row = cur.fetchone()
        changed = row is not None
        if row is None:
            row = _row(cur, schema, ticket_id)
            if not row:
                raise LeaseRefused("LEASE_NOT_FOUND", "not_found", "Ticket not found")
            if live_claim_fields(row, now=now):
                raise LeaseRefused(
                    "LEASE_HELD",
                    "held",
                    "Only the live lease holder can release it",
                    row,
                )
        conn.commit()
        return row, changed
    except BaseException:
        conn.rollback()
        raise
