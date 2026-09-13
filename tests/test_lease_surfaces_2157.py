"""Lease surfaces preserve authenticated identity and fail-soft board effects."""

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest

from tests.test_leases_2157 import row
from tests.test_mcp_tools import _make_mock_mcp, _parse
from tests.test_service import FIXED_TIME, _mock_conn_and_cursor


def test_board_failure_cannot_undo_claim_and_has_named_warning(monkeypatch):
    from stompy_ticketing import lease_actions, leases

    conn, _ = _mock_conn_and_cursor(fetchone_value=row(_renewing=False))
    monkeypatch.setattr(leases, "_now", lambda: FIXED_TIME)
    post, emit = MagicMock(side_effect=RuntimeError("private failure")), MagicMock()
    monkeypatch.setattr(lease_actions, "_board_post", post)
    monkeypatch.setattr(lease_actions, "_emit", emit)
    result = lease_actions.run_action(
        conn, "project", "project", "claim", "51", "Astra", 1
    )
    assert (
        result["status"] == "claimed"
        and result["ticket"]["claimed_by"]["account_id"] == 51
    )
    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()
    assert emit.call_args_list[-1].args[:2] == (
        "warning",
        "ticket_claim_board_post_failed",
    )
    assert "private failure" not in str(emit.call_args_list)


@pytest.mark.asyncio
async def test_mcp_lease_is_write_scoped_and_uses_host_actor(monkeypatch):
    from stompy_ticketing import lease_actions, mcp_tools

    mcp = _make_mock_mcp()
    access = []

    @contextmanager
    def database(project, *, require_write):
        access.append((project, require_write))
        yield object()

    run = MagicMock(return_value={"status": "empty", "ticket": None})
    monkeypatch.setattr(lease_actions, "run_action", run)
    mcp_tools.register_ticketing_tools(
        mcp, database, lambda p: None, lambda p: p, actor_func=lambda: "51"
    )
    result = _parse(
        await mcp._registered_tools["ticket"](
            action="claim_next",
            project="project",
            assignee="Astra",
            agent_label="Claude",
        )
    )
    assert result["status"] == "empty"
    assert access == [("project", True)]
    assert run.call_args.kwargs["actor"] == "51"
    assert run.call_args.kwargs["agent_label"] == "Claude"
    assert run.call_args.kwargs["assignee"] == "Astra"


@pytest.mark.asyncio
async def test_mcp_lease_refusal_preserves_named_holder(monkeypatch):
    from stompy_ticketing import lease_actions, mcp_tools
    from stompy_ticketing.leases import LeaseRefused

    mcp = _make_mock_mcp()

    @contextmanager
    def database(project, *, require_write):
        yield object()

    monkeypatch.setattr(
        lease_actions,
        "run_action",
        MagicMock(side_effect=LeaseRefused("LEASE_HELD", "held", "held", row())),
    )
    mcp_tools.register_ticketing_tools(
        mcp, database, lambda p: None, lambda p: p, actor_func=lambda: "51"
    )
    result = _parse(
        await mcp._registered_tools["ticket"](
            action="claim", project="project", ticket_id=1, agent_label="Claude"
        )
    )
    assert result["error"] == "LEASE_HELD"
    assert result["claimed_by"] == row()["claimed_by"]
