"""Plugin registration entry point for stompy-ticketing.

Provides a single function to wire up MCP tools, REST API routes,
and migration definitions into the Stompy host application.
"""

import logging
from typing import Any, Callable, Dict, List, Optional

from stompy_ticketing.api_routes import configure_routes, router
from stompy_ticketing.mcp_tools import register_ticketing_tools
from stompy_ticketing.migrations import get_archive_migrations, get_ticket_migrations
from stompy_ticketing.schema import get_all_ticket_tables_sql

logger = logging.getLogger(__name__)


def register_plugin(
    mcp_instance: Any,
    api_router: Any,
    get_db_func: Callable,
    check_project_func: Callable,
    get_project_func: Callable,
    resolve_schema_func: Optional[Callable] = None,
    notify_resolution_func: Optional[Callable] = None,
    cache_invalidator_func: Optional[Callable] = None,
    resolve_prefix_func: Optional[Callable] = None,
    get_prefix_func: Optional[Callable] = None,
) -> Dict[str, Any]:
    """One-call plugin registration.

    Wires up:
    1. MCP tools (4 tools) on the mcp_instance
    2. REST API routes on the api_router
    3. Returns migration definitions for the schema patcher

    Args:
        mcp_instance: FastMCP server to register tools on.
        api_router: FastAPI APIRouter to include ticket routes.
        get_db_func: Function(project=None) -> context-manager DB connection.
        check_project_func: Function(project=None) -> error string or None.
        get_project_func: Function(project=None) -> project name string.
        resolve_schema_func: Optional function to resolve project name to schema.
        notify_resolution_func: Optional callback(report, new_status) for bug
            resolution emails on mcp_global tickets.
        cache_invalidator_func: Optional function(project) called after ticket
            writes to invalidate REST response caches. Falls back to no-op.

    Returns:
        Dict with:
            - migrations: List of migration definitions to append
            - schema_sql_func: Function(schema) -> DDL SQL for new projects
    """
    # 1. Register MCP tools
    register_ticketing_tools(
        mcp_instance=mcp_instance,
        get_db_func=get_db_func,
        check_project_func=check_project_func,
        get_project_func=get_project_func,
        resolve_schema_func=resolve_schema_func,
        notify_resolution_func=notify_resolution_func,
        resolve_prefix_func=resolve_prefix_func,
        get_prefix_func=get_prefix_func,
    )
    logger.info("stompy_ticketing: MCP tools registered (4 tools)")

    # 2. Configure and include REST API routes
    configure_routes(
        get_db_func=get_db_func,
        resolve_schema_func=resolve_schema_func,
        cache_invalidator_func=cache_invalidator_func,
    )
    api_router.include_router(router)
    logger.info("stompy_ticketing: REST API routes mounted")

    # 3. Return migration definitions and schema SQL
    migrations = get_ticket_migrations()
    migrations.extend(get_archive_migrations())
    return {
        "migrations": migrations,
        "schema_sql_func": get_all_ticket_tables_sql,
    }
