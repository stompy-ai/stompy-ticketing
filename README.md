# stompy-ticketing

**Python 3.10+** | **78 tests** | **MIT License** | **v0.1.0**

Native ticketing plugin for [Stompy](https://stompy.ai) -- replaces 33 Linear tools with 4 MCP tools (84% context reduction).

---

## What is Stompy?

[Stompy](https://stompy.ai) is a persistent memory system for Claude Code and other AI agents. It gives LLMs durable, project-scoped memory via MCP (Model Context Protocol), backed by PostgreSQL with full-text search, multi-tenant schemas, and a REST API.

Stompy is open source: [github.com/banton/dementia-production](https://github.com/banton/dementia-production)

stompy-ticketing is the first plugin built on the Stompy Plugin SDK, demonstrating how to extend Stompy with domain-specific capabilities without modifying the core server.

## Why stompy-ticketing?

### The Problem

External ticketing tools like Linear expose 30+ MCP tools to the LLM context. Each tool definition consumes tokens. A typical Linear integration adds ~5,000 tokens just for tool schemas -- before any work begins. That is context budget spent on plumbing, not problem-solving.

### The Solution

stompy-ticketing provides a complete ticketing system in 4 MCP tools (~800 tokens). Tickets live inside Stompy's existing database alongside your project memory, eliminating round-trips to external services. State machines enforce valid transitions. Full-text search uses PostgreSQL tsvector for relevance-ranked results.

| Metric | Linear MCP | stompy-ticketing |
|--------|-----------|-----------------|
| MCP tools | ~33 | 4 |
| Schema tokens | ~5,000 | ~800 |
| External API calls | Per operation | None (local DB) |
| Context per operation | ~200 tokens | ~60 tokens |

## Features

### 4 MCP Tools

| Tool | Purpose |
|------|---------|
| `ticket` | Create, read, update, move, list, close tickets |
| `ticket_board` | Kanban or summary dashboard view |
| `ticket_search` | Full-text search with BM25 ranking |
| `ticket_link` | Manage relationships between tickets |

### 10 REST API Endpoints

Full CRUD API mounted at `/projects/{name}/tickets` for programmatic access and web UIs.

### 4 Ticket Types with State Machines

Each ticket type has a dedicated state machine that enforces valid transitions. No invalid state changes are possible.

### Full-Text Search

PostgreSQL `tsvector` with GIN index and automatic trigger-based indexing. Search queries are ranked by relevance using `ts_rank`.

### Audit Trail

Every field change is recorded in `ticket_history` with old value, new value, who changed it, and when.

### Ticket Links

Four relationship types (`blocks`, `parent`, `related`, `duplicate`) with bidirectional query support.

## Quick Start

### Install

```bash
pip install git+https://github.com/banton/stompy-ticketing.git
```

### Register the Plugin

In your Stompy host application (e.g., `server_hosted.py`):

```python
from stompy_ticketing.plugin import register_plugin

result = register_plugin(
    mcp_instance=mcp,              # FastMCP server
    api_router=api_router,         # FastAPI APIRouter
    get_db_func=get_db,            # func(project=None) -> ctx mgr connection
    check_project_func=check_proj, # func(project=None) -> error str | None
    get_project_func=get_proj,     # func(project=None) -> project name str
    resolve_schema_func=resolve,   # Optional: project name -> schema name
)

# result contains:
#   "migrations": list of migration dicts to append to MIGRATIONS
#   "schema_sql_func": callable(schema) -> DDL SQL for new projects
```

The plugin registers 4 MCP tools, mounts 10 REST endpoints, and returns migration definitions for the schema patcher. One call, fully wired.

## State Machines

### task

```
backlog --> in_progress --> done
  |              |
  +-> cancelled  +-> cancelled
```

Initial: `backlog` | Terminal: `done`, `cancelled`

### bug

```
triage --> confirmed --> in_progress --> resolved
  |                          |
  +-> wont_fix               +-> wont_fix
```

Initial: `triage` | Terminal: `resolved`, `wont_fix`

### feature

```
proposed --> approved --> in_progress --> shipped
  |                          |
  +-> rejected               +-> rejected
```

Initial: `proposed` | Terminal: `shipped`, `rejected`

### decision

```
open --> decided
  |
  +-> deferred --> open (reopen)
```

Initial: `open` | Terminal: `decided`, `deferred`

## MCP Tools Reference

### ticket

Primary CRUD and state transitions. Supports 6 actions:

```python
# Create a ticket. The response lists up to 3 OPEN tickets it may duplicate
# (possible_duplicates + one guidance sentence; advisory, never blocking).
ticket(action="create", title="Fix login bug", type="bug", priority="high")

# Check for duplicates without creating anything
ticket(action="create", title="Fix login bug", dry_run=True)

# List tickets with filters
ticket(action="list", type="task", status="in_progress")

# Transition status via state machine
ticket(action="move", ticket_id=1, status="in_progress")

# Get ticket with history and links
ticket(action="get", ticket_id=1)

# Update fields (not status -- use move for that)
ticket(action="update", ticket_id=1, assignee="alice", priority="urgent")

# Close ticket (moves to first reachable terminal status)
ticket(action="close", ticket_id=1)
```

### ticket_board

Dashboard view grouped by status columns:

```python
# Full kanban board with tickets in each column
ticket_board()

# Summary view (counts only, lighter)
ticket_board(view="summary")

# Filter by ticket type
ticket_board(view="kanban", type="bug")
```

### ticket_search

The `regex` post-filter uses **RE2 syntax** (case-insensitive, max 500 characters),
not Python `re`. Look-around and backreferences are refused with an error; there
is no backtracking fallback. RE2 character classes such as `\w` and `\d` are
ASCII; use Unicode properties such as `\p{L}` for Unicode letters. Use `\z`
instead of Python's `\Z`. Ordinary alternation, groups, and repetition work.
The filter still matches title **or** description and preserves card/full output.
Engine memory is limited to 1 MiB per compiled pattern; this does not bound
database retrieval volume or promise a wall-clock deadline.

Full-text search using PostgreSQL tsvector with BM25 ranking:

```python
# Search by keyword
ticket_search(query="authentication bug")

# Search with filters
ticket_search(query="deploy", type="task", status="in_progress", limit=10)
```

### ticket_link

Manage relationships between tickets:

```python
# Create a blocking relationship
ticket_link(action="add", ticket_id=1, target_id=2, link_type="blocks")

# List all links for a ticket
ticket_link(action="list", ticket_id=1)

# Remove a link
ticket_link(action="remove", link_id=5)
```

Link types: `blocks`, `parent`, `related`, `duplicate`

## REST API

All endpoints are mounted at `/projects/{name}/tickets`.

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/projects/{name}/tickets` | Create a ticket |
| `GET` | `/projects/{name}/tickets` | List tickets with filters |
| `GET` | `/projects/{name}/tickets/board` | Kanban or summary board view |
| `GET` | `/projects/{name}/tickets/search` | Full-text search |
| `GET` | `/projects/{name}/tickets/{id}` | Get ticket by ID |
| `PUT` | `/projects/{name}/tickets/{id}` | Update ticket fields |
| `POST` | `/projects/{name}/tickets/{id}/move` | Transition ticket status |
| `POST` | `/projects/{name}/tickets/{id}/links` | Add a ticket link |
| `GET` | `/projects/{name}/tickets/{id}/links` | List ticket links |
| `DELETE` | `/projects/{name}/tickets/{id}/links/{link_id}` | Remove a ticket link |

## Plugin SDK Pattern

stompy-ticketing follows the Stompy Plugin SDK contract. If you want to build your own Stompy plugin, use this as a reference.

### Entry Point

Every plugin exposes a single `register_plugin()` function:

```python
def register_plugin(
    mcp_instance,         # FastMCP server to register tools on
    api_router,           # FastAPI APIRouter to include routes
    get_db_func,          # func(project=None) -> context-manager DB connection
    check_project_func,   # func(project=None) -> error string or None
    get_project_func,     # func(project=None) -> project name string
    resolve_schema_func=None,  # Optional: project name -> schema name
) -> dict:
    # Returns {"migrations": [...], "schema_sql_func": callable}
```

### Schema Pattern

Tables use `{schema}` placeholders for multi-tenant project isolation:

```sql
CREATE TABLE IF NOT EXISTS {schema}.my_table (
    id SERIAL PRIMARY KEY,
    ...
);
```

### Migration Pattern

Migrations follow the Stompy migration format and are appended to the host's migration list. Each migration has an `id`, `description`, `type`, `table`, `schema`, and `spec`:

```python
{
    "id": 26,                           # Unique ID (after Stompy's last migration)
    "description": "create_my_table",
    "type": "custom",
    "table": "my_table",
    "schema": "project",                # "project" = per-project schema
    "spec": {
        "create_if_not_exists": True,
        "sql": "CREATE TABLE IF NOT EXISTS {schema}.my_table (...)"
    }
}
```

### What the Host Provides

The host application (Stompy) provides:
- Database connection management via `get_db_func`
- Project validation via `check_project_func`
- Project name resolution via `get_project_func`
- Schema migration execution
- MCP tool registration on the shared FastMCP instance
- API route mounting on the shared FastAPI router

Plugins do not need to know about connection strings, authentication, or deployment. They receive everything they need through the registration interface.

## Development

### Project Structure

```
stompy-ticketing/
  stompy_ticketing/
    __init__.py          # Package init, exports
    models.py            # Pydantic request/response models
    service.py           # TicketService + state machine logic
    schema.py            # DDL SQL functions with {schema} placeholders
    mcp_tools.py         # 4 MCP tool definitions
    api_routes.py        # FastAPI REST router (10 endpoints)
    migrations.py        # Migration definitions (IDs 26-30)
    plugin.py            # register_plugin() entry point
  tests/
    test_state_machine.py  # State machine transition tests
    test_service.py        # TicketService unit tests (mock DB)
    test_api.py            # API route tests
  pyproject.toml
  CLAUDE.md              # Development guide for AI agents
```

### Running Tests

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run all 78 tests
python3 -m pytest tests/ -v

# Run specific test file
python3 -m pytest tests/test_state_machine.py -v
python3 -m pytest tests/test_service.py -v
python3 -m pytest tests/test_api.py -v
```

All tests use mock database connections with fixed timestamps (`FIXED_TIME = 1700000000.0`). No external services or real databases are required.

### Dependencies

- **Runtime**: `fastapi`, `pydantic>=2.0`
- **Dev**: `pytest`, `pytest-asyncio`, `httpx`
- **Host**: `psycopg2` (provided by Stompy at runtime)

## License

MIT
