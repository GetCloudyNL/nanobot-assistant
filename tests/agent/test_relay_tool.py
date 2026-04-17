"""Tests for the relay_to_ralph guest tool."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from nanobot.agent.tools.relay import RelayToRalphTool


@pytest.mark.asyncio
async def test_relay_forwards_to_owner_chat():
    send = AsyncMock()
    tool = RelayToRalphTool(send_callback=send, owner_chat_id="31657571200")
    tool.set_guest_identity("Tommy van der Heijden", "31618832762")

    result = await tool.execute(message="Kan Ralph me terugbellen?", urgency="normal")

    send.assert_awaited_once()
    outbound = send.await_args.args[0]
    assert outbound.chat_id == "31657571200"
    assert "Tommy van der Heijden" in outbound.content
    assert "terugbellen" in outbound.content
    assert "doorgestuurd" in result.lower()


@pytest.mark.asyncio
async def test_relay_rejects_empty_message():
    send = AsyncMock()
    tool = RelayToRalphTool(send_callback=send, owner_chat_id="31657571200")
    result = await tool.execute(message="   ")
    assert "error" in result.lower()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_relay_requires_owner_chat_id():
    send = AsyncMock()
    tool = RelayToRalphTool(send_callback=send, owner_chat_id="")
    result = await tool.execute(message="hoi")
    assert "error" in result.lower()
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_relay_high_urgency_marker():
    send = AsyncMock()
    tool = RelayToRalphTool(send_callback=send, owner_chat_id="31657571200")
    tool.set_guest_identity("Tommy", "31618832762")

    await tool.execute(message="Nu graag!", urgency="high")

    outbound = send.await_args.args[0]
    assert outbound.content.startswith("🔔")
