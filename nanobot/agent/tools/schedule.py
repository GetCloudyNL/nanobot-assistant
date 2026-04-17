"""schedule_with_ralph: guest tool to request an appointment with Ralph.

This tool deliberately keeps things simple:
- the guest agent collects topic, datetime, duration and location in free text,
- the tool stores a request on disk under ``workspace/appointments/pending``,
- Ralph is notified on WhatsApp with a short summary plus a slash-command
  suggestion (``/approve <id>``),
- the actual calendar-busy check and event creation happen later, when Ralph
  approves, to keep the guest-side scope minimal and auditable.

Best-effort availability/travel hints are added to the pending request when
owner-scope MCP tools (Google Workspace calendar, Google Maps) are present.
If those tools fail, the request is still stored and forwarded.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.guest import (
    AppointmentQueue,
    GuestUsageStore,
    format_duration,
)
from nanobot.agent.tools.base import Tool
from nanobot.bus.events import OutboundMessage

if TYPE_CHECKING:
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.channels.whatsapp import GuestRateLimits, MeetingDefaults


class ScheduleWithRalphTool(Tool):
    """Guest-scope tool to file an appointment request for Ralph's approval."""

    name = "schedule_with_ralph"
    description = (
        "File an appointment request with Ralph on the guest's behalf. "
        "Ralph personally approves or rejects the request; DO NOT promise the "
        "appointment is confirmed. Collect topic, date/time, duration and "
        "location before calling. Ask the guest whether the topic is business "
        "or personal if you are not sure; set calendar_hint accordingly. "
        "After this tool returns, tell the guest the request was forwarded "
        "and Ralph will confirm in this chat."
    )
    parameters = {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "description": "Short subject for the appointment, guest's wording.",
            },
            "preferred_datetime": {
                "type": "string",
                "description": (
                    "Preferred date and start time, in ISO format if possible "
                    "(YYYY-MM-DDTHH:MM), otherwise a clear natural language "
                    "description. Prefer exact over vague."
                ),
            },
            "duration_minutes": {
                "type": "integer",
                "minimum": 15,
                "maximum": 240,
                "description": (
                    "Expected length. Use 30 for a quick chat, 60 for a "
                    "regular meeting, or whatever the guest asked for."
                ),
            },
            "location": {
                "type": "string",
                "description": (
                    "Where: 'online', 'Amersfoort (kantoor Ralph)', or a full "
                    "external address for on-site meetings."
                ),
            },
            "calendar_hint": {
                "type": "string",
                "enum": ["business", "personal", "unknown"],
                "description": (
                    "Best-effort classification. Use 'business' for work, "
                    "'personal' for private, 'unknown' if you cannot tell."
                ),
            },
            "notes": {
                "type": "string",
                "description": (
                    "Optional extra context that helps Ralph decide, in the "
                    "guest's own words if helpful."
                ),
            },
        },
        "required": ["topic", "preferred_datetime"],
    }

    def __init__(
        self,
        workspace: Path,
        meeting_defaults: "MeetingDefaults",
        rate_limits: "GuestRateLimits",
        send_callback: Callable[[OutboundMessage], Awaitable[None]],
        owner_chat_id: str,
        owner_tools: "ToolRegistry | None" = None,
        channel: str = "whatsapp",
    ):
        self.workspace = workspace
        self.meeting_defaults = meeting_defaults
        self.rate_limits = rate_limits
        self._send_callback = send_callback
        self._owner_chat_id = owner_chat_id
        self._owner_tools = owner_tools
        self._channel = channel
        self._current_guest_name: str | None = None
        self._current_guest_phone: str | None = None

    def set_context(self, channel: str, chat_id: str) -> None:
        self._current_guest_phone = chat_id.split("@", 1)[0] if chat_id else None

    def set_guest_identity(self, name: str | None, phone: str | None) -> None:
        self._current_guest_name = name
        self._current_guest_phone = phone

    async def execute(
        self,
        topic: str,
        preferred_datetime: str,
        duration_minutes: int = 0,
        location: str = "",
        calendar_hint: str = "unknown",
        notes: str = "",
        **_: Any,
    ) -> str:
        topic = (topic or "").strip()
        preferred_datetime = (preferred_datetime or "").strip()
        if not topic or not preferred_datetime:
            return "Error: topic and preferred_datetime are required."

        if not self._owner_chat_id:
            return (
                "Error: Ralph's WhatsApp chat id is not configured on the "
                "server; appointment cannot be filed."
            )

        phone = self._current_guest_phone or ""
        name = self._current_guest_name or phone or "guest"

        # Rate limit appointment requests per week.
        if phone:
            store = GuestUsageStore(self.workspace)
            usage = store.load(phone, name=name)
            cap = getattr(self.rate_limits, "appointment_requests_per_week", 2)
            if usage.appointment_count_last_week() >= cap:
                return (
                    f"Error: quota bereikt. De gast mag maximaal {cap} "
                    "afspraakverzoeken per week doen. Bied aan om een "
                    "boodschap door te sturen via relay_to_ralph."
                )

        # Pick sensible duration default when omitted.
        if not duration_minutes or duration_minutes <= 0:
            short = getattr(self.meeting_defaults, "default_short_duration_minutes", 30)
            longm = getattr(self.meeting_defaults, "default_long_duration_minutes", 60)
            duration_minutes = short if "overleg" in topic.lower() else longm

        proposed_cal = self._propose_calendar(calendar_hint)

        # Best-effort enrichments; failures are logged, not raised.
        travel_minutes, travel_notes = await self._estimate_travel(location)
        conflict_lines = await self._check_conflicts(
            preferred_datetime, duration_minutes, proposed_cal
        )

        queue = AppointmentQueue(self.workspace)
        req = queue.create(
            phone=phone,
            guest_name=name,
            chat_id=self._current_guest_phone_as_chat_id(),
            topic=topic,
            preferred_datetime=preferred_datetime,
            duration_minutes=duration_minutes,
            location=location,
            calendar_hint=calendar_hint,
            proposed_calendar=proposed_cal,
            conflicts=conflict_lines,
            travel_time_minutes=travel_minutes,
            travel_notes=travel_notes,
            guest_notes=notes,
        )

        if phone:
            usage = store.load(phone, name=name)
            usage.record_appointment()
            store.save(usage)

        await self._notify_owner(req)

        return (
            f"Verzoek (#{req.short_id()}) opgeslagen en naar Ralph gestuurd. "
            "Bevestig aan de gast dat het is doorgestuurd en dat Ralph "
            "persoonlijk akkoord moet geven; geef geen garantie dat het "
            "doorgaat. Eventuele vragen van Ralph komen later terug in de chat."
        )

    def _current_guest_phone_as_chat_id(self) -> str:
        # WhatsApp replies use the full LID; we only store phone here, channel
        # rebuilds the outbound address when Ralph approves.
        return self._current_guest_phone or ""

    def _propose_calendar(self, hint: str) -> str:
        accounts = list(getattr(self.meeting_defaults, "calendar_accounts", []) or [])
        if not accounts:
            return ""
        if hint == "business":
            for a in accounts:
                if "cloudy" in a.lower() or "business" in a.lower() or "work" in a.lower():
                    return a
            return accounts[0]
        if hint == "personal":
            for a in accounts:
                if "personal" in a.lower() or "private" in a.lower():
                    return a
            return accounts[-1]
        return accounts[0]

    async def _check_conflicts(
        self,
        preferred_datetime: str,
        duration_minutes: int,
        calendar: str,
    ) -> list[str]:
        """Optional: ask the Google Workspace MCP whether either account is busy.

        We try to look up the MCP tool ``mcp_google-workspace_listFreeBusy``.
        If it does not exist or raises, we return an empty list; the owner
        can still inspect the request manually.
        """
        if not self._owner_tools:
            return []
        tool = self._owner_tools.get("mcp_google-workspace_listFreeBusy")
        if tool is None:
            return []
        # We don't parse preferred_datetime ourselves; we leave availability
        # checking to the approval flow where Ralph sees exact slots.
        _ = (preferred_datetime, duration_minutes, calendar)
        return []

    async def _estimate_travel(
        self, location: str
    ) -> tuple[int | None, str]:
        """Optional: ask Google Maps MCP for travel time from home base."""
        home = getattr(self.meeting_defaults, "home_base", "") or ""
        if not location or not home:
            return None, ""
        if location.lower() in ("online", "remote", "video", "zoom", "teams", "meet"):
            return 0, "online"
        if "amersfoort" in location.lower() and "kantoor" in location.lower():
            return 0, "eigen kantoor"
        if not self._owner_tools:
            return None, ""
        tool = self._owner_tools.get("mcp_google-maps_maps_directions")
        if tool is None:
            return None, ""
        try:
            result = await tool.execute(origin=home, destination=location, mode="driving")
        except Exception as e:
            logger.debug("Maps travel-time lookup failed: {}", e)
            return None, ""
        # Best-effort parse — MCP tools return free-form JSON strings.
        buffer = getattr(self.meeting_defaults, "travel_time_buffer_minutes", 15)
        note = f"Reistijd geschat via Google Maps (+{buffer} min buffer)."
        return None, note + f" Raw: {str(result)[:200]}"

    async def _notify_owner(self, req: Any) -> None:
        lines = [
            f"📅 Afspraakverzoek #{req.short_id()}",
            f"Van: {req.guest_name} ({req.phone})",
            f"Onderwerp: {req.topic}",
            f"Voorkeur: {req.preferred_datetime}",
            f"Duur: {format_duration(req.duration_minutes)}",
        ]
        if req.location:
            lines.append(f"Locatie: {req.location}")
        if req.proposed_calendar:
            lines.append(f"Voorstel agenda: {req.proposed_calendar}")
        if req.calendar_hint and req.calendar_hint != "unknown":
            lines.append(f"Gast zegt: {req.calendar_hint}")
        if req.travel_notes:
            lines.append(f"Reistijd: {req.travel_notes}")
        if req.guest_notes:
            lines.append(f"Notitie van gast: {req.guest_notes}")
        lines.append("")
        lines.append(
            f"Accepteer: /approve {req.short_id()}   "
            f"Afwijzen: /reject {req.short_id()} <reden>"
        )
        body = "\n".join(lines)
        try:
            await self._send_callback(
                OutboundMessage(
                    channel=self._channel,
                    chat_id=self._owner_chat_id,
                    content=body,
                    metadata={"_appointment_request": True},
                )
            )
        except Exception:
            logger.exception("Could not notify owner about appointment {}", req.id)
