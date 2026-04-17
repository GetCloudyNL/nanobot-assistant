"""Tests for the schedule_with_ralph guest tool."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from nanobot.agent.guest import AppointmentQueue, GuestUsageStore
from nanobot.agent.tools.schedule import ScheduleWithRalphTool
from nanobot.channels.whatsapp import GuestRateLimits, MeetingDefaults


def _make_tool(tmp_path: Path, *, send=None) -> ScheduleWithRalphTool:
    return ScheduleWithRalphTool(
        workspace=tmp_path,
        meeting_defaults=MeetingDefaults(
            home_base="Laan van Bovenduist 71, Amersfoort",
            calendar_accounts=["personal", "getcloudy"],
            travel_time_buffer_minutes=15,
        ),
        rate_limits=GuestRateLimits(
            messages_per_day=30, appointment_requests_per_week=2
        ),
        send_callback=send or AsyncMock(),
        owner_chat_id="31657571200",
    )


@pytest.mark.asyncio
async def test_schedule_creates_pending_and_notifies_owner(tmp_path: Path):
    send = AsyncMock()
    tool = _make_tool(tmp_path, send=send)
    tool.set_guest_identity("Tommy van der Heijden", "31618832762")

    result = await tool.execute(
        topic="Kennismaking",
        preferred_datetime="2026-04-20T15:00",
        duration_minutes=60,
        location="Amersfoort kantoor",
        calendar_hint="business",
    )

    assert "opgeslagen" in result.lower()
    send.assert_awaited_once()
    notify = send.await_args.args[0]
    assert notify.chat_id == "31657571200"
    assert "Afspraakverzoek" in notify.content
    assert "Tommy van der Heijden" in notify.content

    pending = AppointmentQueue(tmp_path).list_pending()
    assert len(pending) == 1
    assert pending[0].phone == "31618832762"
    assert pending[0].topic == "Kennismaking"
    assert pending[0].duration_minutes == 60


@pytest.mark.asyncio
async def test_schedule_requires_topic_and_datetime(tmp_path: Path):
    tool = _make_tool(tmp_path)
    tool.set_guest_identity("Tommy", "31618832762")

    result = await tool.execute(topic="", preferred_datetime="tomorrow")
    assert "error" in result.lower()


@pytest.mark.asyncio
async def test_schedule_rate_limits_appointments(tmp_path: Path):
    tool = _make_tool(tmp_path)
    tool.set_guest_identity("Tommy", "31618832762")

    # Simulate having already hit the cap.
    store = GuestUsageStore(tmp_path)
    usage = store.load("31618832762", name="Tommy")
    usage.record_appointment()
    usage.record_appointment()
    store.save(usage)

    result = await tool.execute(
        topic="X", preferred_datetime="morgen", duration_minutes=30
    )
    assert "quota" in result.lower()


@pytest.mark.asyncio
async def test_schedule_picks_default_duration(tmp_path: Path):
    tool = _make_tool(tmp_path)
    tool.set_guest_identity("Tommy", "31618832762")

    await tool.execute(
        topic="Kort overleg", preferred_datetime="morgen 10:00"
    )
    req = AppointmentQueue(tmp_path).list_pending()[0]
    # 'overleg' → short default (30 min).
    assert req.duration_minutes == 30


@pytest.mark.asyncio
async def test_schedule_proposes_calendar_based_on_hint(tmp_path: Path):
    tool = _make_tool(tmp_path)
    tool.set_guest_identity("Tommy", "31618832762")

    await tool.execute(
        topic="Zakelijk gesprek",
        preferred_datetime="2026-04-20T15:00",
        duration_minutes=60,
        calendar_hint="business",
    )
    req = AppointmentQueue(tmp_path).list_pending()[0]
    assert "cloudy" in req.proposed_calendar.lower() or req.proposed_calendar == "personal"
