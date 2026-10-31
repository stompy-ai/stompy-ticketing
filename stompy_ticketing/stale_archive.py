"""Legacy stale-ticket sweep, and the one throttle every read runs it through.

The board, ticket list and ticket search sweep at most once per
(database, schema) per process per SWEEP_INTERVAL_SECONDS (STOMPY-3284,
STOMPY-3288). The sweep itself is one statement however many tickets it
archives: the UPDATE's RETURNING feeds the history INSERT.
"""

import time
from typing import Any, Dict, Optional, Tuple

from psycopg2 import sql

#: Reads sweep at most once per (database, schema) per this window.
SWEEP_INTERVAL_SECONDS = 600

_clock = time.monotonic
# minimal: an unlocked dict; a race between two threads costs one extra sweep,
# which is the pre-3284 behaviour. Bounded by the number of (database, schema) pairs.
_last_sweep: Dict[Tuple[Any, str], float] = {}


def _database(conn) -> Optional[Tuple[Any, Any, Any]]:
    """Which database this connection is on: tenants have their own Neon
    databases with same-named schemas, so a schema alone is not a key."""
    try:
        params = conn.get_dsn_parameters()
        return (params.get("host"), params.get("port"), params.get("dbname"))
    except Exception:
        return None


def _sweep_due(conn, schema: str) -> bool:
    database = _database(conn)
    if database is None:
        return True  # unknown database: never let a throttle skip a tenant's sweep
    key, now = (database, schema), _clock()
    last = _last_sweep.get(key)
    if last is not None and now - last < SWEEP_INTERVAL_SECONDS:
        return False
    _last_sweep[key] = now  # an attempt counts: a failing sweep must not retry every read
    return True


def sweep_if_due(service, conn, schema: str) -> None:
    """The read paths' sweep: throttled, and never allowed to fail the read."""
    if _sweep_due(conn, schema):
        try:
            service.archive_stale_tickets(conn, schema)
        except Exception:
            pass


def archive_stale(conn, schema, ttl_seconds, *, clock):
    from stompy_ticketing.service import get_all_terminal_statuses

    cur = conn.cursor()
    try:
        now = clock()
        cutoff = now - ttl_seconds
        all_terminals = get_all_terminal_statuses()
        terminal_placeholders = ", ".join(["%s"] * len(all_terminals))
        cur.execute(
            sql.SQL("""
            WITH archived AS (
                UPDATE {schema}.tickets
                SET archived_at = %s
                WHERE closed_at IS NOT NULL
                  AND closed_at < %s
                  AND archived_at IS NULL
                  AND status IN ({terminals})
                RETURNING id
            )
            INSERT INTO {schema}.ticket_history
                (ticket_id, field_name, old_value, new_value, changed_by, changed_at)
            SELECT id, %s, NULL, %s, %s, %s FROM archived
            RETURNING ticket_id
        """).format(
                schema=sql.Identifier(schema),
                terminals=sql.SQL(terminal_placeholders),
            ),
            [now, cutoff]
            + all_terminals
            + ["archived_at", str(now), "system:auto_archive", now],
        )
        archived = len(cur.fetchall())
        if not archived:
            return 0
        conn.commit()
        return archived
    except Exception:
        conn.rollback()
        raise
