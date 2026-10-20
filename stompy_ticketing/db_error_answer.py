"""ONE answer for an exception a tool's catch-all caught (STOMPY-3089 follow-up).

The four tools used to answer ``f"<Tool> failed: {str(e)[:200]}"``: the DRIVER's
sentence (a relation name, "canceling statement due to statement timeout", a host)
reached the caller. Now the answer is authored — a stable code, a reference id —
and the real error goes to the log beside the same reference. Codes match the
host's (src/mcp_tools/db_error_answer.py), so its classifier reads them as ours:

  * a statement the database cancelled (57014): TOOL_DB_TIMEOUT, retryable;
  * a failed connection: CONNECTION_TIMEOUT; a dropped one: CONNECTION_DROPPED;
  * anything else: the site's own server code, with the reference.

Two refusals keep their old client-class answer, because their text is AUTHORED
(never a driver's) and the host books them by ``error_type``: the host's typed
access denials (STOMPY-2892) and a ValueError the plugin raised itself.
"""

import json
import logging
import uuid

import psycopg2
import psycopg2.errors

from stompy_ticketing.errors import recoverable_error

logger = logging.getLogger(__name__)

#: The host's typed ACCESS denials, matched by name: this package cannot import them.
HOST_REFUSAL_TYPES = frozenset({"ProjectWriteDenied", "ProjectNotFound", "ProjectAccessUnaddressable"})

_RETRY = ["Retry the call", "If it repeats, report the reference in this answer"]
_DROPPED_MARKERS = ("closed", "terminat", "reset by peer", "broken pipe")


def is_statement_cancel(exc: BaseException) -> bool:
    return isinstance(exc, psycopg2.errors.QueryCanceled) or getattr(exc, "pgcode", None) == "57014"


def caught_error_answer(exc: BaseException, tool: str, *, code: str, failed: str, prefix: str) -> str:
    """The authored answer for ``exc``, caught in ``tool``'s catch-all.

    ``code`` / ``failed`` ("Ticket operation failed") are the site's server answer;
    ``prefix`` is the site's old sentence head, kept for the client refusals only.
    """
    name = type(exc).__name__
    if name in HOST_REFUSAL_TYPES or (isinstance(exc, ValueError) and not isinstance(exc, psycopg2.Error)):
        return json.dumps({"error": f"{prefix}: {str(exc)[:200]}", "error_type": name})

    if is_statement_cancel(exc):
        logger.warning(
            "ticket_tool_db_timeout tool=%s error_type=%s", tool, name,
            extra={"tool": tool, "error_type": name},
        )
        return recoverable_error(
            "TOOL_DB_TIMEOUT",
            f"{tool} ran past its time limit and the database stopped it. Retry, or narrow "
            "the request (a filter, a smaller limit).",
            ["Retry the call", "Narrow the request with a filter or a smaller limit"],
        )

    reference = uuid.uuid4().hex[:12]
    # The host formats stdlib records as "%(message)s" (extras are dropped), so the
    # fields go IN the message; the traceback carries the driver's text.
    logger.error(
        "ticket_tool_failed tool=%s error_type=%s reference=%s",
        tool,
        name,
        reference,
        exc_info=exc,
        extra={"tool": tool, "error_type": name, "reference": reference},
    )
    details = {"reference": reference}
    if isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError)):
        dropped = isinstance(exc, psycopg2.InterfaceError) or any(
            m in str(exc).lower() for m in _DROPPED_MARKERS
        )
        what = "was dropped" if dropped else "failed"
        return recoverable_error(
            "CONNECTION_DROPPED" if dropped else "CONNECTION_TIMEOUT",
            f"{tool} could not finish: the database connection {what}. Reference: {reference}.",
            ["Retry the call — a fresh connection is used", "If it repeats, report the reference"],
            details,
        )
    return recoverable_error(code, f"{failed}. Reference: {reference}.", _RETRY, details)
