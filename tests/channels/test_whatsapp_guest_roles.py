"""Tests for WhatsApp guest role resolution and rate-limiting."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from nanobot.channels.whatsapp import (
    GuestRateLimits,
    MeetingDefaults,
    WhatsAppChannel,
    WhatsAppConfig,
    WhatsAppGuestsConfig,
)


def _make_channel(tmp_path: Path) -> WhatsAppChannel:
    cfg = WhatsAppConfig(
        enabled=True,
        allow_from=["31657571200"],
        identities={"31657571200": "Ralph"},
        guests=WhatsAppGuestsConfig(
            allowed={"31618832762": "Tommy van der Heijden"},
            blocked=["31611111111"],
            meeting_defaults=MeetingDefaults(home_base="x"),
            rate_limits=GuestRateLimits(messages_per_day=3),
        ),
    )
    bus = MagicMock()
    bus.publish_inbound = AsyncMock()
    ch = WhatsAppChannel(cfg, bus)
    ch.workspace = tmp_path
    ch._ws = AsyncMock()
    ch._connected = True
    return ch


def test_resolve_role_owner(tmp_path: Path):
    ch = _make_channel(tmp_path)
    role, name = ch._resolve_role("31657571200", "31657571200@s.whatsapp.net", ch.config.guests)
    assert role == "owner"
    assert name == ""


def test_resolve_role_guest(tmp_path: Path):
    ch = _make_channel(tmp_path)
    role, name = ch._resolve_role("31618832762", "31618832762@s.whatsapp.net", ch.config.guests)
    assert role == "guest"
    assert name == "Tommy van der Heijden"


def test_resolve_role_blocked(tmp_path: Path):
    ch = _make_channel(tmp_path)
    role, _ = ch._resolve_role("31611111111", "31611111111@s.whatsapp.net", ch.config.guests)
    assert role is None


def test_resolve_role_unknown(tmp_path: Path):
    ch = _make_channel(tmp_path)
    role, _ = ch._resolve_role("31622222222", "31622222222@s.whatsapp.net", ch.config.guests)
    assert role is None


@pytest.mark.asyncio
async def test_inbound_guest_is_published_with_role(tmp_path: Path):
    ch = _make_channel(tmp_path)
    raw = json.dumps({
        "type": "message",
        "pn": "31618832762@s.whatsapp.net",
        "sender": "31618832762@s.whatsapp.net",
        "content": "hoi karel",
        "id": "m1",
    })
    await ch._handle_bridge_message(raw)

    ch.bus.publish_inbound.assert_awaited_once()
    inbound = ch.bus.publish_inbound.await_args.args[0]
    assert inbound.metadata["role"] == "guest"
    assert inbound.metadata["phone"] == "31618832762"
    assert inbound.metadata["sender_name"] == "Tommy van der Heijden"


@pytest.mark.asyncio
async def test_inbound_unknown_is_silently_dropped(tmp_path: Path):
    ch = _make_channel(tmp_path)
    raw = json.dumps({
        "type": "message",
        "pn": "31622222222@s.whatsapp.net",
        "sender": "31622222222@s.whatsapp.net",
        "content": "who am i",
        "id": "m2",
    })
    await ch._handle_bridge_message(raw)

    ch.bus.publish_inbound.assert_not_awaited()


@pytest.mark.asyncio
async def test_inbound_guest_over_quota_is_declined(tmp_path: Path):
    ch = _make_channel(tmp_path)
    # Burn through quota (3).
    for i in range(3):
        raw = json.dumps({
            "type": "message",
            "pn": "31618832762@s.whatsapp.net",
            "sender": "31618832762@s.whatsapp.net",
            "content": f"msg {i}",
            "id": f"m{i}",
        })
        await ch._handle_bridge_message(raw)

    assert ch.bus.publish_inbound.await_count == 3
    ch._ws.send.reset_mock()

    # Next message should be rate-limited — polite reply, no bus publish.
    raw = json.dumps({
        "type": "message",
        "pn": "31618832762@s.whatsapp.net",
        "sender": "31618832762@s.whatsapp.net",
        "content": "one more",
        "id": "m4",
    })
    await ch._handle_bridge_message(raw)

    assert ch.bus.publish_inbound.await_count == 3
    ch._ws.send.assert_awaited()
    payload = json.loads(ch._ws.send.await_args.args[0])
    assert payload["type"] == "send"
    assert "maximum" in payload["text"].lower()
