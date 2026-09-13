"""Legacy stale-ticket sweep extracted unchanged from TicketService."""

from psycopg2 import sql


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
            SELECT id, type, status FROM {}.tickets
            WHERE closed_at IS NOT NULL
              AND closed_at < %s
              AND archived_at IS NULL
              AND status IN ({})
        """).format(sql.Identifier(schema), sql.SQL(terminal_placeholders)),
            [cutoff] + all_terminals,
        )
        stale = cur.fetchall()
        if not stale:
            return 0
        stale_ids = [row["id"] for row in stale]
        id_placeholders = ", ".join(["%s"] * len(stale_ids))
        cur.execute(
            sql.SQL("""
            UPDATE {}.tickets
            SET archived_at = %s
            WHERE id IN ({})
        """).format(sql.Identifier(schema), sql.SQL(id_placeholders)),
            [now] + stale_ids,
        )
        for ticket in stale:
            cur.execute(
                sql.SQL("""
                INSERT INTO {}.ticket_history
                    (ticket_id, field_name, old_value, new_value, changed_by, changed_at)
                VALUES (%s, %s, %s, %s, %s, %s)
            """).format(sql.Identifier(schema)),
                (
                    ticket["id"],
                    "archived_at",
                    None,
                    str(now),
                    "system:auto_archive",
                    now,
                ),
            )
        conn.commit()
        return len(stale_ids)
    except Exception:
        conn.rollback()
        raise
