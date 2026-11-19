"""Title glob filtering must describe the whole matching set, not one page."""

import fnmatch
import os
from contextlib import closing
from unittest.mock import patch

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

from stompy_ticketing.models import TicketListFilters
from stompy_ticketing.schema import get_tickets_table_sql
from stompy_ticketing.service import TicketService
from stompy_ticketing.ticket_listing import glob_regex
from tests.test_cluster_c_fixes import _register, _run

SCHEMA = "grep_462"
TITLES = [
    "unrelated",
    "auth login",
    "auth logout",
    "auth reset",
    "AUTH upper",
    "100%_done",
    "bracket[",
    "line\nbreak",
    "café",
    "a'b",
    "ab",
    "a\\b",
]


@pytest.fixture
def native_db():
    url = os.environ.get("STOMPY_462_LOCAL_PG")
    if not url:
        pytest.skip("set STOMPY_462_LOCAL_PG to Astra's disposable local PG")
    assert psycopg2.extensions.parse_dsn(url).get("host") == "127.0.0.1"
    with closing(psycopg2.connect(url, cursor_factory=RealDictCursor)) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE SCHEMA grep_462")
            cur.execute("CREATE TABLE grep_462.sessions (id text PRIMARY KEY)")
            cur.execute(get_tickets_table_sql(SCHEMA))
            for i, title in enumerate(TITLES, 1):
                cur.execute(
                    "INSERT INTO grep_462.tickets "
                    "(id,title,description,type,status,priority,created_at,updated_at) "
                    "VALUES (%s,%s,'body','task',%s,'medium',1700000000,%s)",
                    (i, title, "in_progress" if i == 3 else "backlog", 1700000100 - i),
                )
        with patch("stompy_ticketing.service.sweep_if_due"):
            yield conn
        conn.rollback()


def test_matching_page_and_aggregates_precede_pagination(native_db):
    result = TicketService().list_tickets(
        native_db, SCHEMA, TicketListFilters(grep="auth*", limit=1, offset=1)
    )
    assert [t.title for t in result.tickets] == ["auth logout"]
    assert result.total == 3 and result.has_more
    assert result.by_status == {"backlog": 2, "in_progress": 1}
    assert result.by_type == {"task": 3}


def test_no_match_is_empty_without_more(native_db):
    result = TicketService().list_tickets(
        native_db, SCHEMA, TicketListFilters(grep="missing*", limit=1)
    )
    assert result.total == 0 and not result.has_more
    assert result.tickets == [] and result.by_status == {} and result.by_type == {}


def test_mcp_preserves_whole_filtered_totals_on_a_small_page(native_db):
    tools, _ = _register(db_factory=lambda _: native_db)
    original = TicketService().list_tickets
    with patch(
        "stompy_ticketing.mcp_tools.TicketService.list_tickets",
        wraps=lambda conn, schema, filters, **kw: original(conn, SCHEMA, filters, **kw),
    ):
        from toon import decode

        result = decode(_run(tools["ticket"](action="list", grep="auth*", limit=1)))
    assert result["total"] == 3 and result["has_more"]
    assert [t["title"] for t in result["tickets"]] == ["auth login"]
    assert result["by_status"] == {"backlog": 2, "in_progress": 1}


@pytest.mark.parametrize(
    "pattern",
    [
        "a*",
        "a?",
        "[!A]*",
        "[a-c]*",
        "[c-a]*",
        "bracket[",
        "100%_*",
        "*\n*",
        "café",
        "a'b",
        "a\\*",
        "*a*b*",
        "[[]*",
        "[!]*",
        "*",
    ],
)
def test_native_glob_matches_fnmatch(native_db, pattern):
    result = TicketService().list_tickets(
        native_db, SCHEMA, TicketListFilters(grep=pattern, limit=200), fields="full"
    )
    assert [t.title for t in result.tickets] == fnmatch.filter(TITLES, pattern)
    assert result.total == len(fnmatch.filter(TITLES, pattern))


def test_no_grep_keeps_original_page(native_db):
    result = TicketService().list_tickets(native_db, SCHEMA, TicketListFilters(limit=1))
    assert result.total == len(TITLES) and result.has_more
    assert result.tickets[0].title == "unrelated"


def test_glob_combines_with_status_and_full_fields(native_db):
    result = TicketService().list_tickets(
        native_db,
        SCHEMA,
        TicketListFilters(grep="auth*", status="in_progress", limit=1),
        fields="full",
    )
    assert [(t.title, t.description) for t in result.tickets] == [
        ("auth logout", "body")
    ]
    assert result.total == 1 and not result.has_more
    assert result.by_status == {"in_progress": 1} and result.by_type == {"task": 1}


def test_offset_past_matching_set_keeps_its_total(native_db):
    result = TicketService().list_tickets(
        native_db,
        SCHEMA,
        TicketListFilters(grep="auth*", offset=3, limit=1),
    )
    assert result.tickets == [] and result.total == 3 and not result.has_more
    assert result.by_status == {"backlog": 2, "in_progress": 1}


def test_native_bracket_classes_match_oracle(native_db):
    names = ["", "a", "b", "c", "-", "]", "[", "^", "&", "\\", "\n", "aab"]
    patterns = [
        "[a-c]",
        "[!a-c]",
        "[c-a]",
        "[!c-a]",
        "[-a]",
        "[a-]",
        "[]a]",
        "[!]]",
        "[[]",
        "[^a]",
        "[a--b]",
        "[a-b-c]",
        "[a&&b]",
        "[\\]",
        "[",
        "[!]",
        "[a--!]",
        "[[:alpha:]]",
    ]
    with native_db.cursor() as cur:
        for pattern in patterns:
            cur.execute(
                "SELECT value FROM unnest(%s::text[]) AS value "
                'WHERE value COLLATE "C" ~ %s',
                (names, glob_regex(pattern)),
            )
            assert [row["value"] for row in cur.fetchall()] == fnmatch.filter(
                names, pattern
            )


def test_native_mcp_invalid_pattern_is_a_client_refusal(native_db):
    import json

    tools, _ = _register(db_factory=lambda _: native_db)
    original = TicketService().list_tickets
    with (
        patch("stompy_ticketing.ticket_listing.glob_regex", return_value="["),
        patch(
            "stompy_ticketing.mcp_tools.TicketService.list_tickets",
            wraps=lambda conn, schema, filters, **kw: original(
                conn, SCHEMA, filters, **kw
            ),
        ),
    ):
        result = json.loads(_run(tools["ticket"](action="list", grep="a*")))
    assert result["error_type"] == "ValueError"
    assert result["error"] == "Title glob could not be matched; simplify the pattern"
