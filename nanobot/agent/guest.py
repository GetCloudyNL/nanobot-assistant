"""State management for guest WhatsApp access.

Two responsibilities:
1. Rate-limit state per guest (messages/day, appointment requests/week).
2. Approval queue for guest-initiated appointments (pending / approved / rejected).

All state is stored as plain JSON files under the workspace so Ralph can audit
it directly:

  workspace/
    guests/<phone>.json        # per-guest counters
    appointments/
      pending/<uuid>.json
      approved/<uuid>.json
      rejected/<uuid>.json
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from loguru import logger


# --------------------------------------------------------------------------- #
# Rate-limit store                                                            #
# --------------------------------------------------------------------------- #


@dataclass
class GuestUsage:
    """In-file representation of one guest's activity counters."""

    phone: str
    name: str = ""
    message_timestamps: list[float] = field(default_factory=list)
    appointment_timestamps: list[float] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path, phone: str, name: str) -> "GuestUsage":
        if not path.exists():
            return cls(phone=phone, name=name)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                phone=data.get("phone", phone),
                name=data.get("name", name),
                message_timestamps=list(data.get("message_timestamps") or []),
                appointment_timestamps=list(data.get("appointment_timestamps") or []),
            )
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Guest usage file unreadable for {}: {}", phone, e)
            return cls(phone=phone, name=name)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "phone": self.phone,
                    "name": self.name,
                    "message_timestamps": self.message_timestamps,
                    "appointment_timestamps": self.appointment_timestamps,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _prune(self, now: float) -> None:
        day_ago = now - 86_400
        week_ago = now - 7 * 86_400
        self.message_timestamps = [t for t in self.message_timestamps if t >= day_ago]
        self.appointment_timestamps = [
            t for t in self.appointment_timestamps if t >= week_ago
        ]

    def record_message(self) -> None:
        now = time.time()
        self._prune(now)
        self.message_timestamps.append(now)

    def record_appointment(self) -> None:
        now = time.time()
        self._prune(now)
        self.appointment_timestamps.append(now)

    def message_count_last_day(self) -> int:
        self._prune(time.time())
        return len(self.message_timestamps)

    def appointment_count_last_week(self) -> int:
        self._prune(time.time())
        return len(self.appointment_timestamps)


class GuestUsageStore:
    """File-backed store of per-guest usage counters."""

    def __init__(self, workspace: Path):
        self.root = workspace / "guests"

    def path_for(self, phone: str) -> Path:
        safe = "".join(c for c in phone if c.isdigit()) or "unknown"
        return self.root / f"{safe}.json"

    def load(self, phone: str, name: str = "") -> GuestUsage:
        return GuestUsage.load(self.path_for(phone), phone, name)

    def save(self, usage: GuestUsage) -> None:
        usage.save(self.path_for(usage.phone))


# --------------------------------------------------------------------------- #
# Approval queue                                                              #
# --------------------------------------------------------------------------- #


@dataclass
class AppointmentRequest:
    """One pending guest appointment request.

    ``preferred_datetime`` is kept as the guest-provided string (possibly
    natural language like 'donderdag 15:00') so that the approving owner sees
    the exact wording. The agent is instructed to normalise before calling
    the tool, but we do not enforce ISO parsing here.
    """

    id: str
    phone: str
    guest_name: str
    chat_id: str
    topic: str
    preferred_datetime: str
    duration_minutes: int
    location: str = ""
    calendar_hint: str = ""  # "business" | "personal" | "" = agent unsure
    proposed_calendar: str = ""
    conflicts: list[str] = field(default_factory=list)
    travel_time_minutes: int | None = None
    travel_notes: str = ""
    guest_notes: str = ""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "phone": self.phone,
            "guest_name": self.guest_name,
            "chat_id": self.chat_id,
            "topic": self.topic,
            "preferred_datetime": self.preferred_datetime,
            "duration_minutes": self.duration_minutes,
            "location": self.location,
            "calendar_hint": self.calendar_hint,
            "proposed_calendar": self.proposed_calendar,
            "conflicts": list(self.conflicts),
            "travel_time_minutes": self.travel_time_minutes,
            "travel_notes": self.travel_notes,
            "guest_notes": self.guest_notes,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AppointmentRequest":
        return cls(
            id=data["id"],
            phone=data["phone"],
            guest_name=data.get("guest_name", ""),
            chat_id=data.get("chat_id", ""),
            topic=data.get("topic", ""),
            preferred_datetime=data.get("preferred_datetime", ""),
            duration_minutes=int(data.get("duration_minutes", 30)),
            location=data.get("location", ""),
            calendar_hint=data.get("calendar_hint", ""),
            proposed_calendar=data.get("proposed_calendar", ""),
            conflicts=list(data.get("conflicts") or []),
            travel_time_minutes=data.get("travel_time_minutes"),
            travel_notes=data.get("travel_notes", ""),
            guest_notes=data.get("guest_notes", ""),
            created_at=data.get("created_at", ""),
        )

    def short_id(self) -> str:
        """Human-usable short id for slash commands."""
        return self.id[:8]


class AppointmentQueue:
    """Filesystem-backed queue of appointment requests."""

    def __init__(self, workspace: Path):
        self.root = workspace / "appointments"
        self.pending_dir = self.root / "pending"
        self.approved_dir = self.root / "approved"
        self.rejected_dir = self.root / "rejected"

    def _ensure_dirs(self) -> None:
        for d in (self.pending_dir, self.approved_dir, self.rejected_dir):
            d.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        *,
        phone: str,
        guest_name: str,
        chat_id: str,
        topic: str,
        preferred_datetime: str,
        duration_minutes: int,
        location: str = "",
        calendar_hint: str = "",
        proposed_calendar: str = "",
        conflicts: list[str] | None = None,
        travel_time_minutes: int | None = None,
        travel_notes: str = "",
        guest_notes: str = "",
    ) -> AppointmentRequest:
        self._ensure_dirs()
        req = AppointmentRequest(
            id=uuid.uuid4().hex,
            phone=phone,
            guest_name=guest_name,
            chat_id=chat_id,
            topic=topic,
            preferred_datetime=preferred_datetime,
            duration_minutes=duration_minutes,
            location=location,
            calendar_hint=calendar_hint,
            proposed_calendar=proposed_calendar,
            conflicts=list(conflicts or []),
            travel_time_minutes=travel_time_minutes,
            travel_notes=travel_notes,
            guest_notes=guest_notes,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        self._write(self.pending_dir, req)
        return req

    def list_pending(self) -> list[AppointmentRequest]:
        return self._list(self.pending_dir)

    def find_pending(self, short_or_full_id: str) -> AppointmentRequest | None:
        """Resolve either a full uuid or an 8-char prefix."""
        short_or_full_id = short_or_full_id.strip().lower()
        for req in self.list_pending():
            if req.id == short_or_full_id or req.id.startswith(short_or_full_id):
                return req
        return None

    def mark_approved(
        self,
        req: AppointmentRequest,
        *,
        calendar_account: str,
        event_id: str = "",
        note: str = "",
    ) -> None:
        data = req.to_dict()
        data["approved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        data["approved_calendar"] = calendar_account
        data["event_id"] = event_id
        if note:
            data["approve_note"] = note
        self._move(req, self.pending_dir, self.approved_dir, data)

    def mark_rejected(self, req: AppointmentRequest, *, reason: str = "") -> None:
        data = req.to_dict()
        data["rejected_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if reason:
            data["reject_reason"] = reason
        self._move(req, self.pending_dir, self.rejected_dir, data)

    def _write(self, folder: Path, req: AppointmentRequest) -> None:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{req.id}.json"
        path.write_text(
            json.dumps(req.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _list(self, folder: Path) -> list[AppointmentRequest]:
        if not folder.exists():
            return []
        out: list[AppointmentRequest] = []
        for p in sorted(folder.glob("*.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                out.append(AppointmentRequest.from_dict(data))
            except (OSError, json.JSONDecodeError, KeyError) as e:
                logger.warning("Skipping malformed appointment {}: {}", p.name, e)
        return out

    def _move(
        self,
        req: AppointmentRequest,
        from_dir: Path,
        to_dir: Path,
        data: dict[str, Any],
    ) -> None:
        to_dir.mkdir(parents=True, exist_ok=True)
        src = from_dir / f"{req.id}.json"
        dst = to_dir / f"{req.id}.json"
        dst.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            src.unlink()
        except FileNotFoundError:
            pass


def format_duration(minutes: int) -> str:
    if minutes % 60 == 0 and minutes >= 60:
        h = minutes // 60
        return f"{h} uur"
    return f"{minutes} min"


def human_time_since(iso_ts: str) -> str:
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return iso_ts
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta: timedelta = datetime.now(timezone.utc) - ts
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "zojuist"
    if mins < 60:
        return f"{mins} min geleden"
    hours = mins // 60
    if hours < 24:
        return f"{hours} uur geleden"
    days = hours // 24
    return f"{days} dag{'en' if days != 1 else ''} geleden"
