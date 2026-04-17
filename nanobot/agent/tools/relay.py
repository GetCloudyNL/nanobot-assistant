"""relay_to_ralph: guest tool to forward a short message to the owner."""

from __future__ import annotations

from typing import Any, Awaitable, Callable

from loguru import logger

from nanobot.agent.tools.base import Tool
from nanobot.bus.events import OutboundMessage


class RelayToRalphTool(Tool):
    """Send a short message from a guest conversation to Ralph's WhatsApp chat."""

    name = "relay_to_ralph"
    description = (
        "Forward a brief message from the current guest to Ralph directly. "
        "Use this when the guest asks to leave a note or have Ralph call back. "
        "Do NOT use this for appointment requests (use schedule_with_ralph). "
        "The guest does not see the forwarded copy; confirm in words that you "
        "have passed it on."
    )
    parameters = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": (
                    "The note to send to Ralph. Keep it short and in the guest's "
                    "own words; you may add a one-line context prefix."
                ),
            },
            "urgency": {
                "type": "string",
                "enum": ["low", "normal", "high"],
                "description": (
                    "Guest-indicated urgency. 'high' prefixes the message with "
                    "a bell emoji so Ralph sees it stands out."
                ),
            },
        },
        "required": ["message"],
    }

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]],
        owner_chat_id: str,
        channel: str = "whatsapp",
    ):
        self._send_callback = send_callback
        self._owner_chat_id = owner_chat_id
        self._channel = channel
        self._current_guest_name: str | None = None
        self._current_guest_phone: str | None = None

    def set_context(self, channel: str, chat_id: str) -> None:
        """Receive current guest context from the agent loop.

        ``chat_id`` is the guest chat id; we also use it as the guest phone
        when no stored identity is available.
        """
        self._current_guest_phone = chat_id.split("@", 1)[0] if chat_id else None

    def set_guest_identity(self, name: str | None, phone: str | None) -> None:
        self._current_guest_name = name
        self._current_guest_phone = phone

    async def execute(
        self,
        message: str,
        urgency: str = "normal",
        **_: Any,
    ) -> str:
        if not self._owner_chat_id:
            return "Error: Ralph's chat id is not configured; cannot relay."

        message = (message or "").strip()
        if not message:
            return "Error: empty relay message."

        who = self._current_guest_name or self._current_guest_phone or "een gast"
        prefix = "🔔 " if urgency == "high" else "💬 "
        body = f"{prefix}Bericht via Kareltje van {who}:\n\n{message}"

        try:
            await self._send_callback(
                OutboundMessage(
                    channel=self._channel,
                    chat_id=self._owner_chat_id,
                    content=body,
                    metadata={"_relay": True},
                )
            )
        except Exception as e:
            logger.exception("Failed to relay guest message")
            return f"Error: could not deliver message to Ralph: {e}"

        return (
            "Bericht doorgestuurd naar Ralph. Vertel de gast dat het bericht "
            "is aangekomen en dat Ralph erop reageert zodra het lukt."
        )
