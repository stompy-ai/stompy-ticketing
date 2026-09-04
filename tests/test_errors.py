"""Tests for stompy_ticketing.errors (STOMPY-1879).

Every bare tool-level error in mcp_tools.py used to be `{"error": "..."}` —
no code, no recovery guidance — unlike every host-side core tool's
structured shape (`{"success": false, "error": CODE, "message": ...,
"recovery": {"can_retry": bool, "steps": [...]}}`). This module replicates
that exact shape (the host's claude_mcp_utils is not importable here — this
package has no dependency on it and is pip-installed standalone) so an
agent can write ONE error handler across both doors. A contract test in
dementia-production's test suite asserts the same key set. RED first:
written before stompy_ticketing.errors existed.
"""

import json

from stompy_ticketing.errors import mcp_error, not_found_error, recoverable_error


class TestMcpError:
    def test_shape_matches_host_contract(self):
        result = json.loads(mcp_error("INVALID_INPUT", "bad thing"))

        assert result["success"] is False
        assert result["error"] == "INVALID_INPUT"
        assert result["message"] == "bad thing"
        assert "recovery" not in result

    def test_details_are_merged_in(self):
        result = json.loads(mcp_error("INVALID_INPUT", "bad thing", {"ticket_id": 5}))

        assert result["ticket_id"] == 5


class TestRecoverableError:
    def test_shape_matches_host_contract(self):
        result = json.loads(
            recoverable_error("NOT_FOUND", "gone", ["step one", "step two"])
        )

        assert result["success"] is False
        assert result["error"] == "NOT_FOUND"
        assert result["message"] == "gone"
        assert result["recovery"] == {"can_retry": True, "steps": ["step one", "step two"]}

    def test_details_are_merged_in(self):
        result = json.loads(
            recoverable_error("NOT_FOUND", "gone", ["retry"], {"ticket_id": 99999})
        )

        assert result["ticket_id"] == 99999


class TestNotFoundError:
    def test_names_the_entity_and_ref(self):
        result = json.loads(not_found_error("Ticket", 99999))

        assert result["success"] is False
        assert result["error"] == "NOT_FOUND"
        assert "99999" in result["message"]
        assert "recovery" in result
        assert result["recovery"]["steps"]
