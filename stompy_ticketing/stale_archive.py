"""Legacy stale-ticket sweep, and the one throttle every read-path write runs through.

The board, ticket list and ticket search sweep at most once per
(database, schema) per process per SWEEP_INTERVAL_SECONDS (STOMPY-3284,
STOMPY-3288). Search's NULL-tsvector backfill runs through the same throttle
under its own key (STOMPY-3290). The sweep itself is one statement however
many tickets it archives: the UPDATE's RETURNING feeds the history INSERT.
"""

import time
from typing import Any, Callable, Dict, Optional, Tuple

from psycopg2 import sql

#: A read-path write runs at most once per (kind, database, schema) per this window.
SWEEP_INTERVAL_SECONDS = 600

_clock = time.monotonic
# minimal: an unlocked dict; a race between two threads costs one extra write,
# which is the pre-3284 behaviour. Bounded by kinds x (database, schema) pairs.
_last_sweep: Dict[Tuple[str, Any, str], float] = {}


def _database(conn) -> Optional[Tuple[Any, Any, Any]]:
    """Which database this connection is on: tenants have their own Neon
    databases with same-named schemas, so a schema alone is not a key."""
    try:
        params = conn.get_dsn_parameters()
        return (params.get("host"), params.get("port"), params.get("dbname"))
    except Exception:
        return None


def run_if_due(kind: str, conn, schema: str, write: Callable[[], Any]) -> None:
    """Run a read path's write at most once per (kind, database, schema) per
    window, and never let it fail the read.

    ``write`` returning exactly True means work remains (a full batch): the
    window re-arms so the next read continues instead of waiting it out.
    """
    database = _database(conn)
    key, now = (kind, database, schema), _clock()
    if database is not None:  # unknown database: never let a throttle skip a tenant
        last = _last_sweep.get(key)
        if last is not None and now - last < SWEEP_INTERVAL_SECONDS:
            return
        _last_sweep[key] = now  # an attempt counts: a failing write must not retry every read
    try:
        if write() is True:
            _last_sweep.pop(key, None)
    except Exception:
        pass


def sweep_if_due(service, conn, schema: str) -> None:
    """The read paths' stale sweep: throttled, and never allowed to fail the read."""
    run_if_due("stale_sweep", conn, schema, lambda: service.archive_stale_tickets(conn, schema))


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
