"""Stompy Ticketing - A ticketing plugin for Stompy AI Memory."""

from stompy_ticketing.models import (
    TicketCreate,
    TicketUpdate,
    TicketResponse,
    TicketListResponse,
    TicketLinkCreate,
    TicketLinkResponse,
    BoardView,
    CompactTicket,
    SearchResult,
)
from stompy_ticketing.service import TicketService
from stompy_ticketing.schema import (
    get_tickets_table_sql,
    get_ticket_history_table_sql,
    get_ticket_links_table_sql,
)

# Kept in step with pyproject.toml by hand; it had drifted to 0.5.3 while
# the package shipped 0.8.4 (STOMPY-1929).
__version__ = "0.8.5"

__all__ = [
    "TicketService",
    "TicketCreate",
    "TicketUpdate",
    "TicketResponse",
    "TicketListResponse",
    "TicketLinkCreate",
    "TicketLinkResponse",
    "BoardView",
    "SearchResult",
    "get_tickets_table_sql",
    "get_ticket_history_table_sql",
    "get_ticket_links_table_sql",
]
