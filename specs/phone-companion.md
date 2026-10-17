# Phone companion

Status: the remote interface ships as an optional helper, off by default
([user guide](../docs/phone.md)). Push notifications, voice input, a native app
and third-party message channels remain open.

## Shape

- A paired phone is another screen of the same desktop installation, not a second
  agent. Models, tools, memory and sessions stay on the computer.
- The gateway lives in the desktop app's main process, so a phone sees the same
  `Runtime` objects, events and session leases as the window; nothing else ever
  owns a session the window has open.
- It listens on `127.0.0.1` only. `tailscale serve` puts HTTPS on the tailnet in
  front of it; iOS needs a secure origin for a service worker and for push.
- The phone runs the desktop renderer itself. The gateway injects
  `phone/bridge.js` before the app, which implements `window.astra` remotely:
  session calls (`bootstrap`, `create`, `close`, `send`, `query`, delete) go to
  `POST /api/invoke` and the main process applies the window's own checks; events
  arrive as the window's `DesktopEvent`s on one server-sent event stream with
  sequence ids, replayed after a short disconnect and replaced by a reload when
  the gap exceeds what the gateway keeps.
- A phone opening a session does not change the window's selection.

## Identity and trust

- Pairing: an eight-character code shown only in the desktop settings, valid five
  minutes, used once, withdrawn after five wrong tries.
- Each phone generates a non-extractable WebCrypto P-256 key; the desktop keeps
  only the public key. Every sign-in signs a one-time challenge for a 12-hour
  token. Removing a phone ends its tokens and open stream at once.
- Only loopback and `*.ts.net` hosts are answered; cross-origin API calls are
  refused.
- Calls that only make sense at the computer answer on the phone instead:
  choosing files, opening or previewing files, window capture, export, pairing.
  Model-written cards and pages show their source on the phone, where they would
  otherwise share the app's origin.
- A paired phone may answer any approval and switch full access. Every message,
  command, approval and answer sent from a phone is appended to an audit file
  (message text only by length).

## Speech

Speech plays where a conversation is used. Before passing on a message or command,
the desktop sets the session's route (`/voice route phone|mac`). On the phone
route the backend writes each unit's audio as a WAV file the gateway serves and
announces it with a small `voice_audio` event; the phone plays units back to back
with Web Audio after a first touch, and skips speech that has gone stale.

## Open

| Phase | Work | Notes |
|---|---|---|
| Push | Web Push for approvals, questions, finished background work and reminders | VAPID; encrypted payloads; approvals and questions at once, the rest coalesced |
| Voice input | Record on the phone, recognize on the computer | Replies are already spoken on the phone |
| Native app | Share sheet, Shortcuts/Siri, location, calendar and health as tools, APNs | Each capability consented and revocable; nothing written to memory automatically |
| Channels | Telegram and WeChat adapters | Under the existing channel rules: separate sessions, approvals denied |
| Availability | The desktop app must be running; a headless host or wake-on-LAN later | See [Peer Agent workspace](peer-agent-workspace.md) for always-on hosts |

## Non-goals

- Running a model or the agent loop on the phone.
- Reading other apps' notifications.
- Exposing the gateway to the public internet or to other people.
