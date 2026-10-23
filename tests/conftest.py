"""Shared fixtures."""

from __future__ import annotations

import pytest

from .fake_panel import FakePanel


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Load custom_components/ in every HA test."""


@pytest.fixture
async def fake_panel(socket_enabled):
    """A running fake panel on localhost."""
    panel = FakePanel(code="123456")
    await panel.start()
    yield panel
    await panel.stop()
