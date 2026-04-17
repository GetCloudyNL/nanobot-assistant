"""Tests for guest-access state: usage store + appointment queue."""

from __future__ import annotations

import time
from pathlib import Path

from nanobot.agent.guest import (
    AppointmentQueue,
    GuestUsage,
    GuestUsageStore,
    format_duration,
    human_time_since,
)


def test_guest_usage_records_and_prunes(tmp_path: Path):
    store = GuestUsageStore(tmp_path)
    usage = store.load("31618832762", name="Tommy")
    usage.record_message()
    usage.record_message()
    store.save(usage)

    reloaded = store.load("31618832762", name="Tommy")
    assert reloaded.message_count_last_day() == 2

    # Age messages beyond 24h; they must be pruned.
    stale = time.time() - 86_401
    reloaded.message_timestamps = [stale, stale]
    reloaded.record_message()
    assert reloaded.message_count_last_day() == 1


def test_guest_usage_store_path_strips_non_digits(tmp_path: Path):
    store = GuestUsageStore(tmp_path)
    p = store.path_for("+31-618-832.762")
    assert p.name == "31618832762.json"


def test_appointment_queue_lifecycle(tmp_path: Path):
    queue = AppointmentQueue(tmp_path)
    req = queue.create(
        phone="31618832762",
        guest_name="Tommy van der Heijden",
        chat_id="31618832762",
        topic="Koffie",
        preferred_datetime="2026-04-20T15:00",
        duration_minutes=30,
        location="Amersfoort kantoor",
        calendar_hint="business",
        proposed_calendar="getcloudy",
    )

    pending = queue.list_pending()
    assert len(pending) == 1
    assert pending[0].id == req.id

    resolved = queue.find_pending(req.short_id())
    assert resolved is not None and resolved.id == req.id

    queue.mark_approved(req, calendar_account="getcloudy", note="Top!")
    assert queue.list_pending() == []
    approved_files = list((tmp_path / "appointments" / "approved").glob("*.json"))
    assert len(approved_files) == 1


def test_appointment_queue_rejection(tmp_path: Path):
    queue = AppointmentQueue(tmp_path)
    req = queue.create(
        phone="31618832762",
        guest_name="Tommy",
        chat_id="31618832762",
        topic="X",
        preferred_datetime="morgen",
        duration_minutes=30,
    )
    queue.mark_rejected(req, reason="Geen tijd helaas")
    assert queue.list_pending() == []
    rejected_files = list((tmp_path / "appointments" / "rejected").glob("*.json"))
    assert len(rejected_files) == 1


def test_find_pending_returns_none_for_unknown(tmp_path: Path):
    queue = AppointmentQueue(tmp_path)
    assert queue.find_pending("deadbeef") is None


def test_format_duration():
    assert format_duration(30) == "30 min"
    assert format_duration(60) == "1 uur"
    assert format_duration(120) == "2 uur"
    assert format_duration(45) == "45 min"


def test_human_time_since_returns_sensible_string():
    # Empty / bad input is passed through.
    assert human_time_since("") == ""
    assert human_time_since("not-a-timestamp") == "not-a-timestamp"
