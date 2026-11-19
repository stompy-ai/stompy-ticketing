"""Small MCP presentation rules; REST models and concurrency tokens stay numeric."""

from datetime import datetime, timezone

_OUTCOMES = frozenset(
    {
        "created",
        "updated",
        "transitioned",
        "closed",
        "linked",
        "appended",
        "removed",
        "dry_run",
        "claimed",
        "released",
        "archived",
        "unarchived",
        "empty",
    }
)


def present_answer(data):
    if isinstance(data, dict) and "tickets" in data and "query" in data:
        data.pop("query")
        data.pop("include_archived", None)
    if (
        isinstance(data, dict)
        and "id" not in data
        and isinstance(data.get("status"), str)
        and data["status"] in _OUTCOMES
    ):
        data["action"] = data.pop("status")
    return data


def present_ticket_fields(data):
    if isinstance(data, dict):
        if isinstance(data.get("id"), int) and "title" in data and "status" in data:
            for name in ("created_at", "updated_at", "closed_at"):
                value = data.get(name)
                if isinstance(value, (float, int)) and not isinstance(value, bool):
                    try:
                        data[name + "_iso"] = (
                            datetime.fromtimestamp(value, timezone.utc)
                            .isoformat(timespec="seconds")
                            .replace("+00:00", "Z")
                        )
                    except (ValueError, OverflowError, OSError):
                        pass  # Keep the original stored value if it cannot be displayed.
            if "description_chars" in data:
                # Card authors use *_display when available.
                data.pop("created_by", None)
