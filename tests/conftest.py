"""Shared fixtures."""

from unittest.mock import patch

import pytest


@pytest.fixture
def route_links_unread():
    """The REST mutation doors read a ticket's links before writing (STOMPY-3492).
    Tests that script the cursor's fetchone sequence for the WRITE stub that read
    so their scripted rows still reach the write."""
    with patch(
        "stompy_ticketing.service.TicketService.read_links", return_value=([], [])
    ):
        yield
