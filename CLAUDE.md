# stompy-ticketing Development Guide

## Project Overview

Native ticketing system for Stompy MCP. Replaces Linear's 33 tools (~5K tokens) with 4 MCP tools (~800 tokens) — 84% context reduction. Package: `stompy_ticketing`.

Self-dogfooding: use Stompy's own ticket tools (`ticket`, `ticket_board`) with `project="stompy_ticketing"` to track work in this repo.

## Non-derivable contracts (gotchas)

- State machines per ticket type live in `service.py` (`get_initial_status` + transition tables) — four types (task/bug/feature/decision) with DIFFERENT initial and terminal statuses; don't assume a shared workflow.
- Migrations are CUSTOM entries with `{schema}` placeholders, allocated a fixed ID block starting at 26 (after the host's core migrations) — new plugin migrations must extend the block, never renumber, and must ALSO be wired into the host's `definitions.py` (returning them from `register_plugin` is not enough for project schemas).
- REST routes mount at `/projects/{name}/tickets`.
- Ticket refs are dual-format: int | digit-string ("1311", int path) | "PREFIX-123" (case-normalized UPPER). Unknown prefixes and non-int/str garbage raise TicketRefError — see refs.py. display_id decoration is MCP-layer only (contextvar in _safe_json), not part of the service contract.
- This package is pinned by exact git hash in dementia-production's `requirements.txt`; DO's pip cache ignores `@main` — every deployable change needs a version bump in `pyproject.toml` AND a new pinned hash in the host repo.

## Immutable test rules

- No `datetime.now()` / `time.time()` — use the `FIXED_TIME` constant
- No shared mutable fixtures; no random values — deterministic data only
- Mock all DB calls via `_mock_conn_and_cursor` (see `tests/test_service.py` for helpers)

## Skills
- `/test-and-ticket` — run pytest, auto-create Stompy tickets for failures
- `/tdd-cycle` — guided RED-GREEN-REFACTOR workflow
- `/sprint-plan` — create sprint of ticketing tickets
