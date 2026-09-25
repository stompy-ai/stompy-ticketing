"""Advisory duplicate hints on ticket create (STOMPY-2455).

Delta evaluation covered contexts only, so a ticket create never looked for
the ticket it repeated: agents filed STOMPY-2453 minutes after 2451 (same
question, reworded) and 2435 a week after 2315 (same mechanism, reframed).
Every create door now names the nearest OPEN tickets it may duplicate. It is
advisory: it never blocks a create, and its failure never fails one.

The retrieval is the stored ``content_tsvector`` the BM25 ``ticket_search``
already maintains, not a second stack. The score is the mean of two set
cosines (Ochiai, |A∩B| / sqrt(|A|·|B|)) over stemmed lexemes: the whole
text, and the title alone. |A∩B| is ``length(A) + length(B) - length(A||B)``
on stripped tsvectors, so scoring is a C merge per row with no unnest.
Stage 1 scores the body over at most MAX_CANDIDATES most recently updated
open tickets. Stage 2 adds the title score for the best TITLE_RERANK_POOL
only, because ``to_tsvector(title)`` for every row cost ~160 ms on the
stompy project.

Why unweighted, and why 0.33 (measured 2026-09-25, read-only, on the stompy
project: 744 open tickets, the last 77 real creates):
  - The real pairs: 2453 vs 2451 scores 0.717, and the next best pair in the
    whole sample is 0.44. 2435 returns 1715 (0.381), 2315 (0.336) and 2338
    (0.332), all three the same stompy_admin-editable-install bug. A
    genuinely new ticket (2455) peaks at 0.244.
  - IDF weighting did not separate 2435 from 2315 better (2315's long
    appendix dilutes every length-normalised measure). It also cost 135-250
    ms, because document frequencies need a scan of the whole corpus.
  - At 0.33, 16 of the 77 creates (21%) show at least one hint. That
    includes all three real duplicate creates in the window, plus one nobody
    had noticed (2444 vs 1851). The rest are close siblings in the same area.
    At 0.36 the rate halves, but 2315 drops out. Hints are advisory and a
    missed duplicate costs a second worker's time, so recall wins.
  - The limit is lexical: a restatement scores far above the band, but a
    duplicate framed differently sits at its edge. Semantic matching would
    need ticket embeddings, which do not exist yet.

``content_hash`` is the exact-match fast path. A byte-identical re-file
scores 1.0 even when its text stems to nothing (all stopwords, symbols),
which is the one case the lexeme score cannot see.
"""

import hashlib
import logging
from typing import Any, Dict, List, Optional

from psycopg2 import sql

from stompy_ticketing.refs import format_display_id

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.33
MAX_HINTS = 3
# minimal: the oldest open tickets beyond this cap are not compared. Revisit
# when a project holds more than ~1000 OPEN tickets (stompy: 744).
MAX_CANDIDATES = 1000
TITLE_RERANK_POOL = 25
GUIDANCE = "If this is one of these, append to it and close this one as a duplicate."

_QUERY = """
WITH q AS MATERIALIZED (
    SELECT strip(to_tsvector('english', %(title)s)) AS tv,
           strip(to_tsvector('english', %(title)s || ' ' || %(description)s)) AS bv
), pool AS (
    SELECT c.id, c.title, c.status, coalesce(c.content_hash = %(hash)s, false) AS exact,
           (length(c.bb) + length(q.bv) - length(c.bb || q.bv))
               / sqrt(greatest(length(c.bb) * length(q.bv), 1)) AS body_sim
    FROM q, (
        SELECT id, title, status, content_hash,
               coalesce(strip(content_tsvector), ''::tsvector) AS bb
        FROM {tickets}
        WHERE archived_at IS NULL AND NOT (status = ANY(%(terminal)s)) AND id <> %(exclude)s
        ORDER BY updated_at DESC
        LIMIT %(max_candidates)s
    ) c
    ORDER BY exact DESC, body_sim DESC
    LIMIT %(pool)s
)
SELECT pool.id, pool.title, pool.status,
       CASE WHEN pool.exact THEN 1.0 ELSE
           (pool.body_sim + (length(t.tt) + length(q.tv) - length(t.tt || q.tv))
               / sqrt(greatest(length(t.tt) * length(q.tv), 1))) / 2
       END AS similarity
FROM pool, q, LATERAL (SELECT strip(to_tsvector('english', pool.title)) AS tt) t
ORDER BY similarity DESC, pool.id DESC
"""


def content_hash(title: str, description: Optional[str]) -> str:
    """The value create stores in tickets.content_hash."""
    return hashlib.sha256(f"{title}|{description or ''}".encode()).hexdigest()[:16]


def find_possible_duplicates(
    conn,
    schema: str,
    title: str,
    description: Optional[str],
    exclude_id: Optional[int] = None,
    prefix: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """Up to MAX_HINTS open tickets scoring >= SIMILARITY_THRESHOLD, best first.

    Returns [] when nothing is similar and None when the check itself failed,
    so a door can say "unavailable" instead of implying "no duplicates". Never
    raises. Run it after the create commits (or with nothing pending): a
    failure rolls the connection back.
    """
    from stompy_ticketing.service import get_all_terminal_statuses

    try:
        cur = conn.cursor()
        cur.execute(
            sql.SQL(_QUERY).format(tickets=sql.SQL("{}.tickets").format(sql.Identifier(schema))),
            {
                "title": title,
                "description": description or "",
                "hash": content_hash(title, description),
                "terminal": get_all_terminal_statuses(),
                "exclude": exclude_id if exclude_id is not None else -1,
                "max_candidates": MAX_CANDIDATES,
                "pool": TITLE_RERANK_POOL,
            },
        )
        rows = cur.fetchall()
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        logger.warning(
            "ticket_duplicate_hints_failed",
            extra={"schema": schema, "error_type": type(e).__name__},
        )
        return None
    hints = [r for r in rows if float(r["similarity"]) >= SIMILARITY_THRESHOLD][:MAX_HINTS]
    return [
        {
            "id": r["id"],
            "display_id": format_display_id(prefix, r["id"]),
            "title": r["title"],
            "status": r["status"],
            "similarity": round(float(r["similarity"]), 2),
        }
        for r in hints
    ]


def response_fields(hints: Optional[List[Dict[str, Any]]]) -> Dict[str, Any]:
    """The keys a create response gains from find_possible_duplicates."""
    if hints is None:
        return {"duplicate_check": "unavailable"}
    if not hints:
        return {"possible_duplicates": []}
    return {"possible_duplicates": hints, "duplicate_guidance": GUIDANCE}
