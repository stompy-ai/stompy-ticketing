"""Targeted archive/restore decisions. Host owns admission for counted restores."""

import time

from psycopg2 import sql

from stompy_ticketing.models import BatchItemResult, BatchOperationResult
from stompy_ticketing.ticket_projection import row_to_response


class ArchiveRefused(ValueError):
    def __init__(self, code, reason, message):
        self.code, self.reason = code, reason
        super().__init__(message)

    def payload(self):
        return {"error": self.code, "message": str(self)}


def _eligible(row):
    from stompy_ticketing.service import get_terminal_statuses

    return row.get("archived_at") is not None or row["status"] in {
        "parked",
        *get_terminal_statuses(row["type"]),
    }


def _refuse(row, *, restoring=False):
    if row is None:
        raise ArchiveRefused("ARCHIVE_NOT_FOUND", "not_found", "Ticket does not exist")
    if restoring and row.get("archived_at") is None:
        raise ArchiveRefused(
            "ARCHIVE_NOT_ALLOWED", "not_archived", "Ticket is not archived"
        )
    if not restoring and row.get("archived_at") is not None:
        raise ArchiveRefused(
            "ARCHIVE_NOT_ALLOWED", "already_archived", "Ticket is already archived"
        )
    if not restoring and not _eligible(row):
        raise ArchiveRefused(
            "ARCHIVE_NOT_ALLOWED",
            "not_parked_or_closed",
            "Archive requires a parked or closed ticket; park it first",
        )


def _emit(level, event, **fields):
    # The existing host event adapter is configured once during registration.
    from stompy_ticketing.lease_actions import _emit as host_event

    host_event(level, event, **fields)


def _single(conn, schema, ticket_id, changed_by, *, restoring, project=None):
    project = project or schema
    try:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("SELECT * FROM {}.tickets WHERE id = %s FOR UPDATE").format(
                    sql.Identifier(schema)
                ),
                (ticket_id,),
            )
            row = cur.fetchone()
            _refuse(row, restoring=restoring)
            old = row.get("archived_at")
            changed = (old is not None) if restoring else (old is None)
            if changed:
                now = time.time()
                value = None if restoring else now
                cur.execute(
                    sql.SQL(
                        "UPDATE {}.tickets SET archived_at = %s, "
                        "updated_at = GREATEST(COALESCE(updated_at, 0) + 0.000001, %s) "
                        "WHERE id = %s RETURNING *"
                    ).format(sql.Identifier(schema)),
                    (value, now, ticket_id),
                )
                row = cur.fetchone()
                if row is None:
                    raise ArchiveRefused(
                        "ARCHIVE_NOT_FOUND", "not_found", "Ticket disappeared"
                    )
                cur.execute(
                    sql.SQL(
                        "INSERT INTO {}.ticket_history "
                        "(ticket_id, field_name, old_value, new_value, changed_by, changed_at) "
                        "VALUES (%s, %s, %s, %s, %s, %s)"
                    ).format(sql.Identifier(schema)),
                    (
                        ticket_id,
                        "archived_at",
                        str(old) if old is not None else None,
                        str(value) if value is not None else None,
                        changed_by,
                        now,
                    ),
                )
            conn.commit()
    except Exception as exc:
        conn.rollback()
        if isinstance(exc, ArchiveRefused):
            _emit(
                "warning",
                "ticket_archive_refused",
                ticket=ticket_id,
                project=project,
                reason=exc.reason,
                restoring=restoring,
            )
        raise
    if changed:
        _emit(
            "info",
            "ticket_unarchived" if restoring else "ticket_archived",
            ticket=ticket_id,
            project=project,
            actor=changed_by,
        )
    return row_to_response(row)


class ArchiveMixin:
    def archive_stale_tickets(
        self, conn, schema: str, ttl_seconds: int = 1_209_600
    ) -> int:
        """Legacy no-id sweep; semantics unchanged by targeted archival."""
        from stompy_ticketing.stale_archive import archive_stale
        from stompy_ticketing.service import time as legacy_clock

        return archive_stale(conn, schema, ttl_seconds, clock=legacy_clock.time)

    def archive_ticket(self, conn, schema, ticket_id, changed_by=None, *, project=None):
        return _single(
            conn, schema, ticket_id, changed_by, restoring=False, project=project
        )

    def unarchive_ticket(
        self, conn, schema, ticket_id, changed_by=None, *, project=None
    ):
        return _single(
            conn, schema, ticket_id, changed_by, restoring=True, project=project
        )

    def batch_archive(
        self, conn, schema, ticket_ids, confirm=False, changed_by=None, *, project=None
    ):
        project = project or schema
        if not 1 <= len(ticket_ids) <= 50 or any(
            type(i) is not int or i <= 0 for i in ticket_ids
        ):
            _emit(
                "warning",
                "ticket_archive_refused",
                project=project,
                reason="invalid_ids",
                restoring=False,
            )
            raise ArchiveRefused(
                "ARCHIVE_INVALID_IDS",
                "invalid_ids",
                "Provide 1..50 positive ticket IDs",
            )
        ids = sorted(set(ticket_ids))
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL(
                    "SELECT * FROM {}.tickets WHERE id = ANY(%s) ORDER BY id"
                ).format(sql.Identifier(schema)),
                (ids,),
            )
            rows = {row["id"]: row for row in cur.fetchall()}
        results = []
        for ticket_id in ids:
            row = rows.get(ticket_id)
            try:
                if confirm:
                    # Each committed item re-reads/locks its current state;
                    # preview is never authority for a later write.
                    result = self.archive_ticket(
                        conn, schema, ticket_id, changed_by, project=project
                    )
                    status = result.status
                else:
                    _refuse(row)
                    status = row["status"]
                results.append(
                    BatchItemResult(
                        ticket_id=ticket_id,
                        success=True,
                        old_status=status,
                        new_status=status,
                    )
                )
            except ArchiveRefused as exc:
                results.append(
                    BatchItemResult(
                        ticket_id=ticket_id, success=False, error=f"{exc.code}: {exc}"
                    )
                )
        succeeded = sum(item.success for item in results)
        return BatchOperationResult(
            action="batch_archive",
            total=len(ids),
            succeeded=succeeded,
            failed=len(ids) - succeeded,
            results=results,
            dry_run=not confirm,
        )
