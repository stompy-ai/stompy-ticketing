"""The list ceiling is a real transaction-local PostgreSQL timeout."""

import os
import threading
import uuid
from contextlib import closing
from unittest.mock import patch

import psycopg2
import pytest
from psycopg2 import sql
from psycopg2.extras import RealDictCursor

from stompy_ticketing.models import TicketListFilters
from stompy_ticketing.schema import get_all_ticket_tables_sql
from stompy_ticketing.service import TicketService
from tests.test_cluster_c_fixes import _register, _run
from tests.tool_response import parse_tool_response


@pytest.fixture
def bounded_db():
    url = os.environ.get("STOMPY_3794_LOCAL_PG")
    if not url:
        pytest.skip("set STOMPY_3794_LOCAL_PG to disposable local PostgreSQL")
    params = psycopg2.extensions.parse_dsn(url)
    assert params.get("host") == "127.0.0.1"
    name = "astra3794_" + uuid.uuid4().hex
    with closing(psycopg2.connect(url)) as admin:
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
            cur.execute("SELECT datname FROM pg_database WHERE datname=%s", (name,))
            assert cur.fetchone() == (name,)
        params["dbname"] = name
        try:
            with closing(
                psycopg2.connect(**params, cursor_factory=RealDictCursor)
            ) as conn:
                with conn.cursor() as cur:
                    cur.execute("CREATE SCHEMA bounded")
                    cur.execute("CREATE TABLE bounded.sessions(id text PRIMARY KEY)")
                    cur.execute(get_all_ticket_tables_sql("bounded"))
                    cur.execute(
                        "INSERT INTO bounded.tickets "
                        "(title,type,status,priority,created_at,updated_at) "
                        "VALUES ('auth one','task','backlog','medium',1,1)"
                    )
                conn.commit()
                yield conn, params
        finally:
            with admin.cursor() as cur:
                cur.execute(
                    sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                        sql.Identifier(name)
                    )
                )
                cur.execute("SELECT datname FROM pg_database WHERE datname=%s", (name,))
                assert cur.fetchone() is None


def setting(conn):
    with conn.cursor() as cur:
        cur.execute("SHOW statement_timeout")
        return cur.fetchone()["statement_timeout"]


def test_real_list_bound_is_local_and_survives_maintenance_commit(bounded_db):
    conn, _ = bounded_db
    service = TicketService()
    with patch(
        "stompy_ticketing.service.sweep_if_due", side_effect=lambda *a: conn.commit()
    ):
        answer = service.list_tickets(conn, "bounded", TicketListFilters(grep="auth*"))
    assert [t.title for t in answer.tickets] == ["auth one"]
    assert setting(conn) == "5s"
    conn.commit()
    assert setting(conn) == "0"


@pytest.mark.parametrize("door", ["service", "mcp"])
def test_real_lock_wait_is_cancelled_and_connection_is_reusable(bounded_db, door):
    conn, params = bounded_db
    ready, release = threading.Event(), threading.Event()

    def hold():
        with closing(psycopg2.connect(**params)) as blocker:
            with blocker.cursor() as cur:
                cur.execute("LOCK TABLE bounded.tickets IN ACCESS EXCLUSIVE MODE")
            ready.set()
            release.wait(8)  # a reverted timeout must fail, never hang the harness
            blocker.rollback()

    worker = threading.Thread(target=hold)
    worker.start()
    assert ready.wait(2)
    try:
        with patch("stompy_ticketing.service.sweep_if_due"):
            if door == "service":
                with pytest.raises(ValueError, match="(?i)timed out.*narrow"):
                    TicketService().list_tickets(
                        conn, "bounded", TicketListFilters(grep="auth*")
                    )
            else:
                service = TicketService()
                original = service.list_tickets
                service.list_tickets = lambda c, s, f, **kw: original(
                    c, "bounded", f, **kw
                )
                tools, _ = _register(mock_svc=service, db_factory=lambda _: conn)
                answer = parse_tool_response(
                    _run(tools["ticket"](action="list", grep="auth*"))
                )
                assert answer["error"] == "INVALID_ARGUMENT"
                assert (
                    "timed out" in answer["message"] and "narrow" in answer["message"]
                )
        assert setting(conn) == "0"  # service rolled the cancelled transaction back
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS alive")
            assert cur.fetchone()["alive"] == 1
    finally:
        release.set()
        worker.join(10)
        assert not worker.is_alive()


def test_native_normal_glob_control_preserves_page_and_totals(bounded_db):
    conn, _ = bounded_db
    result = TicketService().list_tickets(
        conn, "bounded", TicketListFilters(grep="auth*")
    )
    assert result.total == 1 and not result.has_more
    assert [t.title for t in result.tickets] == ["auth one"]
