# WhatsApp Guest Access

Nanobot supports a second, restricted role on the WhatsApp channel: **guest**.
Guests are pre-approved visitors who can chat with the assistant and request
appointments with the owner, but cannot trigger anything else. All other
unknown numbers are silently dropped.

## Roles

| Role | Where listed | Access |
|------|--------------|--------|
| owner | `channels.whatsapp.allowFrom` | full toolset, slash commands |
| guest | `channels.whatsapp.guests.allowed` | `message`, `relay_to_ralph`, `schedule_with_ralph` |
| blocked | `channels.whatsapp.guests.blocked` | always dropped |
| unknown | — | silently dropped (no reply) |

Guest access is **only** active when at least one entry is present in
`guests.allowed` or `guests.blocked`. Otherwise the channel keeps its
original single-owner behaviour.

## Config

Add to `~/.nanobot/config.json` under `channels.whatsapp`:

```json
{
  "channels": {
    "whatsapp": {
      "enabled": true,
      "allowFrom": ["31657571200"],
      "identities": {"31657571200": "Ralph van der Linden"},
      "guests": {
        "allowed": {
          "31618832762": "Tommy van der Heijden"
        },
        "blocked": [],
        "meetingDefaults": {
          "homeBase": "Laan van Bovenduist 71, Amersfoort",
          "travelTimeBufferMinutes": 15,
          "calendarAccounts": ["personal", "getcloudy"],
          "defaultShortDurationMinutes": 30,
          "defaultLongDurationMinutes": 60
        },
        "rateLimits": {
          "messagesPerDay": 30,
          "appointmentRequestsPerWeek": 2
        }
      }
    }
  }
}
```

Changes to the guest list require a nanobot restart.

## Persona

The guest persona lives in `workspace/GUEST_SOUL.md`. It is copied from the
bundled template on first start. Edit that file to tune Kareltje's guest
behaviour (tone, scope limits, refusal style).

For owner conversations the existing `SOUL.md` + `USER.md` + `AGENTS.md`
stack is used, untouched.

## Appointment flow

1. Guest asks for an appointment.
2. Agent collects topic, preferred date/time, duration and location.
3. Agent calls `schedule_with_ralph`. A JSON file is written to
   `workspace/appointments/pending/<uuid>.json` and a summary is pushed to
   Ralph's own WhatsApp chat with an `/approve <id>` / `/reject <id>`
   suggestion.
4. Ralph replies with `/approve <id>` (optionally followed by a free text
   confirmation) or `/reject <id> <reden>`. The guest is notified
   automatically and the request moves to `approved/` or `rejected/`.
5. Ralph creates the actual calendar entry himself, optionally by asking the
   owner-scope agent to use the Google Workspace MCP tools.

Use `/pending` to list all open requests.
Use `/guests` to show the configured guest list.

## Security

- Guest conversations are isolated: the guest system prompt omits memory,
  skills, `USER.md`, `AGENTS.md` and owner identity. It only loads
  `GUEST_SOUL.md`.
- Every incoming guest message is wrapped in `<guest_message untrusted="true">`
  before being handed to the model, and a reminder is injected telling the
  model to treat the contents as data rather than instructions.
- Slash commands are not available to guests; any message starting with `/`
  is refused.
- Rate limits prevent abuse: per-guest counters (messages/day,
  appointments/week) live in `workspace/guests/<phone>.json`. When a guest
  exceeds a cap they receive a polite rate-limit reply without the agent
  being invoked.
- Memory consolidation is skipped for guest sessions.
