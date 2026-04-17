"""Owner-side slash commands for handling guest appointment requests.

These commands are registered when the WhatsApp channel has guest access
enabled. They operate on the appointment queue under
``workspace/appointments`` and notify the guest chat when decisions are
made.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from nanobot.agent.guest import (
    AppointmentQueue,
    format_duration,
    human_time_since,
)
from nanobot.bus.events import OutboundMessage
from nanobot.command.router import CommandContext, CommandRouter

if TYPE_CHECKING:
    pass


async def cmd_pending(ctx: CommandContext) -> OutboundMessage:
    """List pending guest appointment requests."""
    workspace = _workspace(ctx)
    queue = AppointmentQueue(workspace)
    pending = queue.list_pending()
    if not pending:
        body = "Geen openstaande afspraakverzoeken."
    else:
        lines = [f"Openstaand ({len(pending)}):"]
        for req in pending:
            age = human_time_since(req.created_at) if req.created_at else ""
            lines.append(
                f"• #{req.short_id()} — {req.guest_name}: {req.topic} "
                f"({req.preferred_datetime}, {format_duration(req.duration_minutes)})"
                + (f" — {age}" if age else "")
            )
        lines.append("")
        lines.append("Gebruik /approve <id> of /reject <id> [reden].")
        body = "\n".join(lines)
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id, content=body,
    )


async def cmd_approve(ctx: CommandContext) -> OutboundMessage:
    """Approve an appointment request and notify the guest.

    Syntax: ``/approve <id> [optional free text confirmation]``
    The optional trailing text is passed to the guest as the confirmation
    wording. Actual calendar event creation is left to the approver (Ralph
    can create it directly via the agent with Google Workspace MCP tools);
    this command focuses on the guest-facing handshake and audit trail.
    """
    args = (ctx.args or "").strip()
    parts = args.split(maxsplit=1)
    if not parts:
        return _reply(ctx, "Gebruik: /approve <id> [bevestigingstekst]")
    short_id = parts[0]
    note = parts[1] if len(parts) > 1 else ""

    workspace = _workspace(ctx)
    queue = AppointmentQueue(workspace)
    req = queue.find_pending(short_id)
    if req is None:
        return _reply(ctx, f"Geen openstaand verzoek met id '{short_id}'.")

    queue.mark_approved(req, calendar_account=req.proposed_calendar, note=note)

    # Build guest-facing confirmation.
    confirm_lines = [
        f"Top {req.guest_name}! Ralph heeft de afspraak bevestigd.",
        f"Onderwerp: {req.topic}",
        f"Wanneer: {req.preferred_datetime}",
        f"Duur: {format_duration(req.duration_minutes)}",
    ]
    if req.location:
        confirm_lines.append(f"Locatie: {req.location}")
    if note:
        confirm_lines.append("")
        confirm_lines.append(note)
    guest_text = "\n".join(confirm_lines)

    if req.chat_id:
        try:
            await ctx.loop.bus.publish_outbound(
                OutboundMessage(
                    channel=ctx.msg.channel,
                    chat_id=_rebuild_whatsapp_chat_id(req.chat_id),
                    content=guest_text,
                    metadata={"_appointment_approved": True},
                )
            )
        except Exception:
            logger.exception("Could not notify guest about approval {}", req.id)

    owner_body = (
        f"✅ Afspraak #{req.short_id()} goedgekeurd. "
        f"Bericht naar {req.guest_name} verstuurd.\n"
        "Vergeet niet de kalender-entry aan te maken (ik kan 'm voor je "
        "prikken als je 't vraagt)."
    )
    return _reply(ctx, owner_body)


async def cmd_reject(ctx: CommandContext) -> OutboundMessage:
    """Reject a pending appointment request with an optional reason."""
    args = (ctx.args or "").strip()
    parts = args.split(maxsplit=1)
    if not parts:
        return _reply(ctx, "Gebruik: /reject <id> [reden]")
    short_id = parts[0]
    reason = parts[1] if len(parts) > 1 else ""

    workspace = _workspace(ctx)
    queue = AppointmentQueue(workspace)
    req = queue.find_pending(short_id)
    if req is None:
        return _reply(ctx, f"Geen openstaand verzoek met id '{short_id}'.")

    queue.mark_rejected(req, reason=reason)

    guest_lines = [
        f"Hoi {req.guest_name}, de voorgestelde afspraak "
        f"({req.preferred_datetime}) lukt Ralph helaas niet.",
    ]
    if reason:
        guest_lines.append(reason)
    guest_lines.append(
        "Je mag gerust een ander moment voorstellen, dan leg ik 't opnieuw voor."
    )
    guest_text = "\n".join(guest_lines)

    if req.chat_id:
        try:
            await ctx.loop.bus.publish_outbound(
                OutboundMessage(
                    channel=ctx.msg.channel,
                    chat_id=_rebuild_whatsapp_chat_id(req.chat_id),
                    content=guest_text,
                    metadata={"_appointment_rejected": True},
                )
            )
        except Exception:
            logger.exception("Could not notify guest about rejection {}", req.id)

    return _reply(
        ctx,
        f"❌ Afspraak #{req.short_id()} afgewezen. {req.guest_name} "
        "is op de hoogte gesteld.",
    )


async def cmd_guests(ctx: CommandContext) -> OutboundMessage:
    """Show the configured guest list (display-only)."""
    loop = ctx.loop
    cfg = getattr(loop, "whatsapp_guests", None)
    if cfg is None or not getattr(cfg, "allowed", {}):
        return _reply(ctx, "Geen gasten geconfigureerd.")
    lines = ["Geconfigureerde gasten:"]
    for phone, name in cfg.allowed.items():
        lines.append(f"• {name} ({phone})")
    blocked = getattr(cfg, "blocked", [])
    if blocked:
        lines.append("")
        lines.append("Geblokkeerd:")
        for phone in blocked:
            lines.append(f"• {phone}")
    lines.append("")
    lines.append(
        "Wijzig via config.channels.whatsapp.guests; daarna een herstart."
    )
    return _reply(ctx, "\n".join(lines))


def _reply(ctx: CommandContext, text: str) -> OutboundMessage:
    return OutboundMessage(
        channel=ctx.msg.channel, chat_id=ctx.msg.chat_id, content=text,
        metadata={"render_as": "text"},
    )


def _workspace(ctx: CommandContext):
    return ctx.loop.workspace


def _rebuild_whatsapp_chat_id(stored: str) -> str:
    """Turn a stored phone into a WhatsApp chat id if needed.

    The schedule tool saves either a phone or a full LID. WhatsApp accepts
    the bare phone (``31…``) and resolves it internally, so we pass it
    through as-is.
    """
    return stored


def register_guest_commands(router: CommandRouter) -> None:
    """Register owner-side guest-handling commands."""
    router.exact("/pending", cmd_pending)
    router.exact("/guests", cmd_guests)
    router.prefix("/approve ", cmd_approve)
    router.prefix("/reject ", cmd_reject)
