# Phone companion (optional)

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/phone.md)

An optional helper, off by default: nothing listens until you turn it on in
Settings → 手机.

A paired iPhone runs the desktop app's own interface remotely: the same session
list, conversations, approvals and questions, command palette, models, modes,
permissions (Full access included), file changes and history, laid out for a
phone. Models, tools and memory stay on the Mac; the phone is another screen of
the same Astra, and what it does acts on the Mac's backend. The design is in
`specs/phone-companion.md`.

The phone opens on the **personal thread** ([desktop](gui.md#personal-thread)): the one
conversation the desktop keeps apart from your projects, where scheduled results arrive.
The sidebar switches to your other sessions.

Read-aloud follows you: replies to a conversation used from the phone are spoken on the
phone, and on the Mac again once it is used there (see [voice output](voice.md)).

Some things only make sense at the Mac and say so on the phone: choosing files or
folders (attachments, projects), opening or previewing files, window capture,
exporting a session, and pairing phones. Model-written cards and pages show their
source on the phone instead of running, because there they would share the app's
own origin. Theme, drafts and other display choices stay on the phone; session
names and pins come from the desktop.

## Set up

1. Install Tailscale on the Mac and the iPhone and sign in to the same tailnet.
   In the Tailscale admin console, turn on MagicDNS and HTTPS certificates.
2. Keep the Mac awake while you are away: plug it in, and in System Settings →
   Battery → Options, prevent automatic sleeping on power adapter when the
   display is off. Closing the lid sleeps a MacBook unless an external display is
   connected.
3. In Astra's desktop app, open Settings → 手机 and turn on 允许配对的手机连接.
   The desktop listens on `127.0.0.1:8930` only (`ASTRA_PHONE_PORT` changes it).
4. Once, in a terminal on the Mac: `tailscale serve --bg 8930`. Tailscale then
   serves the app over HTTPS at your Mac's tailnet name, shown in Settings.
   Do not use Tailscale Funnel; it would put the page on the public internet.
5. On the iPhone, open that address in Safari, tap Share → Add to Home Screen,
   and open Astra from the Home Screen.
6. In Settings on the Mac, click 添加手机 and type the eight-character code on the
   phone within five minutes.

## Notifications

On the phone, open Settings and turn on 手机通知. iOS offers notifications only to
the app opened from the Home Screen (iOS 16.4 or later), and asks once; if it was
refused, turn it back on in the iPhone's Settings → Notifications → Astra.

The phone is notified of what the Mac would notify you of while you are not at
it: an approval or a question at once, and finished long replies, scheduled-job
results and the budget notice gathered into one notification a minute. Nothing
is sent while the phone has Astra open, and you count as away from the Mac when
its window is not in front or the Mac has been idle or locked for two minutes.
Tapping a notification opens that conversation.

Notifications use standard Web Push: the Mac encrypts each one for that phone
(RFC 8291) and signs the request with its own key (RFC 8292, kept in
`phone-push-key.json` in Astra's desktop data folder), so Apple's push service
carries only ciphertext. The Mac sends them itself, so it must be awake and online;
the phone does not need Tailscale to receive them. Settings on the Mac shows which
phones have notifications on; 移除 also stops them.

## How it is protected

- Nothing listens on a network address: only Tailscale reaches the desktop, and
  only devices in your tailnet reach Tailscale.
- Each phone makes its own signing key, which cannot be read out of the phone's
  storage. The Mac keeps the public key, and every sign-in signs a one-time
  challenge; there is no shared password. A pairing code works once, lasts five
  minutes and is withdrawn after five wrong tries.
- Settings lists paired phones. 移除 ends that phone's connection at once and
  stops it from signing in again; it has to be paired anew.
- Every message, command, approval and question answer sent from a phone is
  recorded in `phone-audit.jsonl` in Astra's desktop data folder, with the phone,
  the time and the decision; message text is not kept, only its length.
- Turning off 允许配对的手机连接 stops the gateway. The desktop app must be running
  for the phone to connect; the tray icon keeps it running without a window.
