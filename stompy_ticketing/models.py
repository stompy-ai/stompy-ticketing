"""Pydantic request/response models for stompy-ticketing."""

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# --------------------------------------------------------------------------- #
# Enums                                                                       #
# --------------------------------------------------------------------------- #


class TicketType(str, Enum):
    task = "task"
    bug = "bug"
    feature = "feature"
    decision = "decision"


class Priority(str, Enum):
    urgent = "urgent"
    high = "high"
    medium = "medium"
    low = "low"
    none = "none"


class LinkType(str, Enum):
    blocks = "blocks"
    parent = "parent"
    related = "related"
    duplicate = "duplicate"


class ContextLinkType(str, Enum):
    implements = "implements"
    references = "references"
    updates = "updates"
    related = "related"


# --------------------------------------------------------------------------- #
# Request models                                                              #
# --------------------------------------------------------------------------- #


class TicketCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    description: Optional[str] = None
    type: TicketType = TicketType.task
    priority: Priority = Priority.medium
    assignee: Optional[str] = None
    tags: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = None


class TicketUpdate(BaseModel):
    title: Optional[str] = Field(None, min_length=1, max_length=500)
    description: Optional[str] = None
    type: Optional[TicketType] = None
    priority: Optional[Priority] = None
    assignee: Optional[str] = None
    tags: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None


class TicketTransition(BaseModel):
    status: str


class TicketLinkCreate(BaseModel):
    target_id: int
    link_type: LinkType = LinkType.related


class ContextLinkCreate(BaseModel):
    context_label: str
    context_version: str = "latest"
    link_type: ContextLinkType = ContextLinkType.related


class BatchMoveRequest(BaseModel):
    ticket_ids: List[int] = Field(..., min_length=1, max_length=50)
    status: str
    confirm: bool = False
    note: Optional[str] = None


class BatchCloseRequest(BaseModel):
    ticket_ids: List[int] = Field(..., min_length=1, max_length=50)
    confirm: bool = False
    note: Optional[str] = None


class TicketListFilters(BaseModel):
    type: Optional[TicketType] = None
    status: Optional[str] = None
    priority: Optional[Priority] = None
    assignee: Optional[str] = None
    search: Optional[str] = None
    tags: Optional[str] = None  # Comma-separated, match ANY tag via LIKE
    limit: int = Field(20, ge=1, le=200)
    offset: int = Field(0, ge=0)
    include_archived: bool = False


# --------------------------------------------------------------------------- #
# Response models                                                             #
# --------------------------------------------------------------------------- #


class TicketHistoryEntry(BaseModel):
    id: int
    field_name: str
    old_value: Optional[str] = None
    new_value: Optional[str] = None
    changed_by: Optional[str] = None  # str(internal_id) — identity, never a note (1594)
    changed_by_display: Optional[str] = None  # resolved at read time by the host
    changed_at: Optional[float] = None


class TicketLinkResponse(BaseModel):
    id: int
    source_id: int
    target_id: int
    link_type: str
    created_at: Optional[float] = None
    # Denormalized target info for display
    target_title: Optional[str] = None
    target_status: Optional[str] = None


class TicketResponse(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    description_preview: Optional[str] = Field(
        None,
        description=(
            "Card-sized excerpt of description. Set ONLY on board responses, "
            "and there whenever a description exists; equals description when "
            "nothing was cut. A display value must not wear a data name "
            "(STOMPY-1519)."
        ),
    )
    type: str
    status: str
    priority: str
    assignee: Optional[str] = None
    tags: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None
    session_id: Optional[str] = None
    created_by: Optional[str] = None  # str(internal_id) of the filer (STOMPY-1594)
    created_by_display: Optional[str] = None  # resolved at read time by the host
    created_at: Optional[float] = None
    updated_at: Optional[float] = None
    closed_at: Optional[float] = None
    archived_at: Optional[float] = None
    history: List[TicketHistoryEntry] = Field(default_factory=list)
    links: List[TicketLinkResponse] = Field(default_factory=list)
    context_links: List["ContextLinkResponse"] = Field(default_factory=list)


class ContextLinkResponse(BaseModel):
    id: int
    ticket_id: int
    context_label: str
    context_version: str
    link_type: str
    created_at: Optional[float] = None
    # Denormalized for display (fetched from ticket row)
    ticket_title: Optional[str] = None
    ticket_status: Optional[str] = None


# Resolve forward reference for context_links in TicketResponse
TicketResponse.model_rebuild()


class TicketListResponse(BaseModel):
    tickets: List[TicketResponse]
    total: int
    limit: int = 20
    offset: int = 0
    has_more: bool = False
    by_status: Optional[Dict[str, int]] = None
    by_type: Optional[Dict[str, int]] = None


class CompactTicket(BaseModel):
    """Minimal ticket representation for compact board views."""

    id: int
    title: str
    type: str
    status: str
    priority: str
    assignee: Optional[str] = None


class BoardColumn(BaseModel):
    status: str
    count: int
    tickets: List[TicketResponse] = Field(default_factory=list)
    compact_tickets: List[CompactTicket] = Field(default_factory=list)
    has_more: bool = False
    # Number of tickets in this column not included in the response due to
    # the per-column limit. count == len(tickets) + truncated_count.
    truncated_count: int = 0


class BoardView(BaseModel):
    columns: List[BoardColumn]
    total: int
    type_filter: Optional[str] = None
    include_archived: bool = False
    archived_count: int = 0
    view: str = "kanban"
    limit_per_column: Optional[int] = None


class SearchResult(BaseModel):
    tickets: List[TicketResponse]
    total: int
    query: str
    include_archived: bool = False


class BatchItemResult(BaseModel):
    ticket_id: int
    success: bool
    error: Optional[str] = None
    old_status: Optional[str] = None
    new_status: Optional[str] = None


class BatchOperationResult(BaseModel):
    action: str
    total: int
    succeeded: int
    failed: int
    results: List[BatchItemResult] = Field(default_factory=list)
    dry_run: bool = True
