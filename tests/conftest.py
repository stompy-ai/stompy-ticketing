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


@pytest.fixture
def known_ticket_card_columns():
    """Scripted query tests already assume a complete tickets table.

    Native old-table/lease tests keep the real catalog reader; opt in only
    when a test scripts rows/counts rather than catalog results.
    """
    with patch("stompy_ticketing.board_view._ticket_columns", return_value=None):
        yield
