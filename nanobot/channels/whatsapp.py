"""WhatsApp channel implementation using Node.js bridge."""

import asyncio
import hashlib
import json
import mimetypes
import os
import shutil
import subprocess
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal

from loguru import logger
from pydantic import Field

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.schema import Base


def _path_looks_like_audio(path: str) -> bool:
    """Guess if a saved media path is voice/audio (bridge may label as [Document])."""
    mime, _ = mimetypes.guess_type(path)
    if mime and mime.startswith("audio/"):
        return True
    ext = Path(path).suffix.lower().lstrip(".")
    return ext in ("ogg", "opus", "m4a", "mp3", "wav", "aac", "webm")


class MeetingDefaults(Base):
    """Defaults for guest appointment requests."""

    home_base: str = ""  # Address used as reference for travel time calculation
    travel_time_buffer_minutes: int = 15
    # Google Calendar account IDs (as configured in google-workspace MCP). Both are
    # checked for conflicts; the agent proposes one based on topic.
    calendar_accounts: list[str] = Field(default_factory=list)
    default_short_duration_minutes: int = 30
    default_long_duration_minutes: int = 60


class GuestRateLimits(Base):
    """Per-guest rate limits."""

    messages_per_day: int = 30
    appointment_requests_per_week: int = 2


class WhatsAppGuestsConfig(Base):
    """Guest access configuration."""

    allowed: dict[str, str] = Field(default_factory=dict)  # phone -> display name
    blocked: list[str] = Field(default_factory=list)
    meeting_defaults: MeetingDefaults = Field(default_factory=MeetingDefaults)
    rate_limits: GuestRateLimits = Field(default_factory=GuestRateLimits)


class WhatsAppConfig(Base):
    """WhatsApp channel configuration."""

    enabled: bool = False
    bridge_url: str = "ws://localhost:3001"
    bridge_token: str = ""
    allow_from: list[str] = Field(default_factory=list)
    # Optional: map phone numbers (or LIDs) to human-readable names so the agent
    # knows who is talking. Example: {"31657571200": "Ralph van der Linden"}
    identities: dict[str, str] = Field(default_factory=dict)
    group_policy: Literal["open", "mention"] = "open"  # "open" responds to all, "mention" only when @mentioned
    # Guest access: limited scope (scheduling and relaying messages only).
    guests: WhatsAppGuestsConfig = Field(default_factory=WhatsAppGuestsConfig)


class WhatsAppChannel(BaseChannel):
    """
    WhatsApp channel that connects to a Node.js bridge.

    The bridge uses @whiskeysockets/baileys to handle the WhatsApp Web protocol.
    Communication between Python and Node.js is via WebSocket.
    """

    name = "whatsapp"
    display_name = "WhatsApp"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return WhatsAppConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = WhatsAppConfig.model_validate(config)
        super().__init__(config, bus)
        self._ws = None
        self._connected = False
        self._processed_message_ids: OrderedDict[str, None] = OrderedDict()

    def is_allowed(self, sender_id: str) -> bool:
        """Allow owners (allow_from) and configured guests."""
        if super().is_allowed(sender_id):
            return True
        guests_cfg = getattr(self.config, "guests", None)
        if guests_cfg is None:
            return False
        allowed = set((guests_cfg.allowed or {}).keys())
        blocked = set(guests_cfg.blocked or [])
        return sender_id in allowed and sender_id not in blocked

    async def login(self, force: bool = False) -> bool:
        """
        Set up and run the WhatsApp bridge for QR code login.

        This spawns the Node.js bridge process which handles the WhatsApp
        authentication flow. The process blocks until the user scans the QR code
        or interrupts with Ctrl+C.
        """
        from nanobot.config.paths import get_runtime_subdir

        try:
            bridge_dir = _ensure_bridge_setup()
        except RuntimeError as e:
            logger.error("{}", e)
            return False

        env = {**os.environ}
        if self.config.bridge_token:
            env["BRIDGE_TOKEN"] = self.config.bridge_token
        env["AUTH_DIR"] = str(get_runtime_subdir("whatsapp-auth"))

        logger.info("Starting WhatsApp bridge for QR login...")
        try:
            subprocess.run(
                [shutil.which("npm"), "start"], cwd=bridge_dir, check=True, env=env
            )
        except subprocess.CalledProcessError:
            return False

        return True

    async def start(self) -> None:
        """Start the WhatsApp channel by connecting to the bridge."""
        import websockets

        # Rebuild ~/.nanobot/bridge when bundled source (e.g. whatsapp.ts) changed — otherwise
        # users keep an old bridge without voice download after upgrading nanobot.
        try:
            await asyncio.to_thread(_ensure_bridge_setup)
        except Exception as e:
            logger.warning("WhatsApp bridge sync skipped: {}", e)

        bridge_url = self.config.bridge_url

        logger.info("Connecting to WhatsApp bridge at {}...", bridge_url)

        self._running = True

        while self._running:
            try:
                async with websockets.connect(bridge_url) as ws:
                    self._ws = ws
                    # Send auth token if configured
                    if self.config.bridge_token:
                        await ws.send(
                            json.dumps({"type": "auth", "token": self.config.bridge_token})
                        )
                    self._connected = True
                    logger.info("Connected to WhatsApp bridge")

                    # Listen for messages
                    async for message in ws:
                        try:
                            await self._handle_bridge_message(message)
                        except Exception as e:
                            logger.error("Error handling bridge message: {}", e)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                self._ws = None
                logger.warning("WhatsApp bridge connection error: {}", e)

                if self._running:
                    logger.info("Reconnecting in 5 seconds...")
                    await asyncio.sleep(5)

    async def stop(self) -> None:
        """Stop the WhatsApp channel."""
        self._running = False
        self._connected = False

        if self._ws:
            await self._ws.close()
            self._ws = None

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through WhatsApp."""
        if not self._ws or not self._connected:
            logger.warning("WhatsApp bridge not connected")
            return

        chat_id = msg.chat_id

        if msg.content:
            try:
                payload = {"type": "send", "to": chat_id, "text": msg.content}
                await self._ws.send(json.dumps(payload, ensure_ascii=False))
            except Exception as e:
                logger.error("Error sending WhatsApp message: {}", e)
                raise

        for media_path in msg.media or []:
            try:
                mime, _ = mimetypes.guess_type(media_path)
                mime = mime or "application/octet-stream"
                payload: dict[str, Any] = {
                    "type": "send_media",
                    "to": chat_id,
                    "filePath": media_path,
                    "mimetype": mime,
                    "fileName": media_path.rsplit("/", 1)[-1],
                }
                # Opus/ogg needs an explicit codec hint for WhatsApp clients
                # (esp. iOS) to render an inline, scrubbable audio player.
                if media_path.lower().endswith((".ogg", ".opus")):
                    payload["mimetype"] = "audio/ogg; codecs=opus"
                logger.info(
                    "Sending WhatsApp media to {}: path={} mime={} ptt={}",
                    chat_id,
                    media_path,
                    payload["mimetype"],
                    payload.get("ptt", False),
                )
                await self._ws.send(json.dumps(payload, ensure_ascii=False))
                logger.debug("WhatsApp media payload dispatched to bridge")
            except Exception as e:
                logger.error("Error sending WhatsApp media {}: {}", media_path, e)
                raise

    async def _handle_bridge_message(self, raw: str) -> None:
        """Handle a message from the bridge."""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Invalid JSON from bridge: {}", raw[:100])
            return

        msg_type = data.get("type")

        if msg_type == "message":
            # Incoming message from WhatsApp
            # Deprecated by whatsapp: old phone number style typically: <phone>@s.whatspp.net
            pn = data.get("pn", "")
            # New LID sytle typically:
            sender = data.get("sender", "")
            content = (data.get("content") or "").strip()
            message_id = data.get("id", "")

            if message_id:
                if message_id in self._processed_message_ids:
                    return
                self._processed_message_ids[message_id] = None
                while len(self._processed_message_ids) > 1000:
                    self._processed_message_ids.popitem(last=False)

            # Extract just the phone number or lid as chat_id
            is_group = data.get("isGroup", False)
            was_mentioned = data.get("wasMentioned", False)

            if is_group and getattr(self.config, "group_policy", "open") == "mention":
                if not was_mentioned:
                    return

            user_id = pn if pn else sender
            sender_id = user_id.split("@")[0] if "@" in user_id else user_id
            logger.info("Sender {}", sender)

            # Media paths from bridge (images, documents, video, voice audio file)
            media_paths = list(data.get("media") or [])

            # Voice: transcribe via Groq Whisper (requires providers.groq.api_key in config)
            treat_as_voice = content == "[Voice Message]" or (
                content == "[Document]"
                and media_paths
                and _path_looks_like_audio(media_paths[0])
            )
            if treat_as_voice:
                if media_paths:
                    audio_path = media_paths[0]
                    transcript = await self.transcribe_audio(audio_path)
                    if transcript:
                        content = transcript
                        logger.info(
                            "Voice message transcribed from {}: {}...",
                            sender_id,
                            transcript[:80],
                        )
                        media_paths = []
                    else:
                        content = (
                            "[Voice Message: transcriptie mislukt — zet een Groq API key in "
                            "config onder providers.groq.apiKey]"
                        )
                        media_paths = []
                else:
                    content = "[Voice Message: geen audiobestand ontvangen van de bridge]"

            # Build content tags matching Telegram's pattern: [image: /path] or [file: /path]
            if media_paths:
                for p in media_paths:
                    mime, _ = mimetypes.guess_type(p)
                    media_type = "image" if mime and mime.startswith("image/") else "file"
                    media_tag = f"[{media_type}: {p}]"
                    content = f"{content}\n{media_tag}" if content else media_tag

            identities = getattr(self.config, "identities", {}) or {}
            guests_cfg = getattr(self.config, "guests", None)

            # Guest-mode is only active when explicitly configured. If no
            # guests are listed we keep the original behaviour (single-owner
            # allowlist) so configs without guests/blocked sections behave
            # exactly as before.
            guests_configured = bool(
                guests_cfg
                and (getattr(guests_cfg, "allowed", None) or getattr(guests_cfg, "blocked", None))
            )

            role: str | None = "owner"
            guest_name = ""
            if guests_configured:
                role, guest_name = self._resolve_role(sender_id, user_id, guests_cfg)
                if role is None:
                    logger.debug(
                        "WhatsApp message silently dropped from unknown/blocked {}",
                        sender_id,
                    )
                    return

            sender_name = (
                identities.get(sender_id)
                or identities.get(user_id)
                or guest_name
                or None
            )

            if role == "guest":
                if not self._guest_rate_limit_ok(sender_id, sender_name or ""):
                    logger.info(
                        "Rate limit hit for guest {}; sending polite decline",
                        sender_id,
                    )
                    await self._send_rate_limit_notice(sender)
                    return

            await self._handle_message(
                sender_id=sender_id,
                chat_id=sender,  # Use full LID for replies
                content=content,
                media=media_paths,
                metadata={
                    "message_id": message_id,
                    "timestamp": data.get("timestamp"),
                    "is_group": data.get("isGroup", False),
                    "sender_name": sender_name,
                    "role": role,
                    "phone": sender_id,
                },
            )
            return

        if msg_type == "status":
            status = data.get("status")
            logger.info("WhatsApp status: {}", status)
            if status == "connected":
                self._connected = True
            elif status == "disconnected":
                self._connected = False
            return

        if msg_type == "qr":
            logger.info("Scan QR code in the bridge terminal to connect WhatsApp")
            return

        if msg_type == "error":
            logger.error("WhatsApp bridge error: {}", data.get("error"))
            return

    def _resolve_role(
        self,
        sender_id: str,
        user_id: str,
        guests_cfg: Any,
    ) -> tuple[str | None, str]:
        """Return (role, guest_display_name)."""
        if sender_id in (self.config.allow_from or []) or user_id in (
            self.config.allow_from or []
        ):
            return "owner", ""
        if guests_cfg is None:
            return None, ""
        blocked = set(guests_cfg.blocked or [])
        if sender_id in blocked or user_id in blocked:
            return None, ""
        allowed = dict(guests_cfg.allowed or {})
        if sender_id in allowed:
            return "guest", allowed[sender_id]
        if user_id in allowed:
            return "guest", allowed[user_id]
        return None, ""

    def _guest_rate_limit_ok(self, phone: str, name: str) -> bool:
        """Record an inbound guest message; return False if over quota."""
        workspace = self._resolve_workspace()
        if workspace is None:
            return True  # Don't block if we can't persist state

        from nanobot.agent.guest import GuestUsageStore

        store = GuestUsageStore(workspace)
        usage = store.load(phone, name=name)
        limits = getattr(
            self.config.guests, "rate_limits", None
        ) if hasattr(self.config, "guests") else None
        cap = getattr(limits, "messages_per_day", 30) if limits else 30
        if usage.message_count_last_day() >= cap:
            return False
        usage.record_message()
        store.save(usage)
        return True

    async def _send_rate_limit_notice(self, chat_id: str) -> None:
        """Send a polite rate-limit message without invoking the agent."""
        if not self._ws or not self._connected:
            return
        try:
            payload = {
                "type": "send",
                "to": chat_id,
                "text": (
                    "Dankjewel voor je bericht! Je hebt voor vandaag het maximum "
                    "bereikt. Ralph krijgt dit door; hij neemt morgen contact op "
                    "als het nodig is."
                ),
            }
            await self._ws.send(json.dumps(payload, ensure_ascii=False))
        except Exception as e:
            logger.debug("Could not send rate-limit notice: {}", e)

    def _resolve_workspace(self) -> Path | None:
        """Resolve the workspace directory for guest-state storage."""
        return self.workspace


def _find_bridge_source() -> Path | None:
    """Resolve bundled bridge source directory (package or repo layout)."""
    current_file = Path(__file__)
    pkg_bridge = current_file.parent.parent / "bridge"
    src_bridge = current_file.parent.parent.parent / "bridge"
    if (pkg_bridge / "package.json").exists():
        return pkg_bridge
    if (src_bridge / "package.json").exists():
        return src_bridge
    return None


def _bridge_source_fingerprint(source: Path) -> str:
    """Hash of bridge TS sources that affect runtime behaviour (voice, media)."""
    h = hashlib.sha256()
    for rel in ("src/whatsapp.ts", "src/server.ts", "package.json"):
        p = source / rel
        if p.exists():
            h.update(rel.encode())
            h.update(p.read_bytes())
    return h.hexdigest()


def _ensure_bridge_setup() -> Path:
    """
    Ensure the WhatsApp bridge is set up and built.

    Returns the bridge directory. Raises RuntimeError if npm is not found
    or bridge cannot be built.
    """
    from nanobot.config.paths import get_bridge_install_dir

    user_bridge = get_bridge_install_dir()
    marker = user_bridge / ".bridge_src_sha256"

    npm_path = shutil.which("npm")
    if not npm_path:
        raise RuntimeError("npm not found. Please install Node.js >= 18.")

    source = _find_bridge_source()
    if not source:
        raise RuntimeError(
            "WhatsApp bridge source not found. "
            "Try reinstalling: pip install --force-reinstall nanobot"
        )

    fingerprint = _bridge_source_fingerprint(source)
    dist_ok = (user_bridge / "dist" / "index.js").exists()
    if dist_ok and marker.exists():
        try:
            if marker.read_text(encoding="utf-8").strip() == fingerprint:
                return user_bridge
        except OSError:
            pass

    logger.info("Setting up WhatsApp bridge (source changed or first install)...")
    user_bridge.parent.mkdir(parents=True, exist_ok=True)
    if user_bridge.exists():
        shutil.rmtree(user_bridge)
    shutil.copytree(source, user_bridge, ignore=shutil.ignore_patterns("node_modules", "dist"))

    logger.info("  Installing dependencies...")
    subprocess.run([npm_path, "install"], cwd=user_bridge, check=True, capture_output=True)

    logger.info("  Building...")
    subprocess.run([npm_path, "run", "build"], cwd=user_bridge, check=True, capture_output=True)

    try:
        marker.write_text(fingerprint, encoding="utf-8")
    except OSError:
        pass

    logger.info("Bridge ready")
    return user_bridge
