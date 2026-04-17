# Tool Usage Notes

Tool signatures are provided automatically via function calling.
This file documents non-obvious constraints and usage patterns.

## exec — Safety Limits

- Commands have a configurable timeout (default 60s)
- Dangerous commands are blocked (rm -rf, format, dd, shutdown, etc.)
- Output is truncated at 10,000 characters
- `restrictToWorkspace` config can limit file access to the workspace

## cron — Scheduled Reminders

- Please refer to cron skill for usage.

## podcast — Audio Summaries

- Produces a two-host audio summary (opus/ogg by default) from a URL or text block.
- Requires `tools.podcast.enabled = true` in config, an OpenAI API key, and `ffmpeg` on PATH.
- The tool returns the saved audio path. To deliver it, call `message` with `media=["/path.ogg"]`.
- Length auto-adapts to source substance — do not pad or under-summarize.
- Typical trigger phrases: "maak een podcast van…", "luister naar dit…", "vat samen als audio…".

## Guest mode (WhatsApp only)

When a WhatsApp message arrives from a number listed under
`channels.whatsapp.guests.allowed`, a restricted toolset is used and the
`GUEST_SOUL.md` persona takes over. Guests only see:

- `message` — to reply to them.
- `relay_to_ralph` — forward a short note to Ralph's own WhatsApp chat.
- `schedule_with_ralph` — file an appointment request (Ralph approves via
  `/approve <id>` or `/reject <id> [reason]`).

Never expose implementation details or other tools to guests.
