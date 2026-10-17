# Phone companion

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/phone.md)

A paired iPhone can follow the sessions open in the desktop app on your Mac: the
session list, each conversation as it streams, and what is waiting for you. The
phone is another screen of the same Astra; models, tools and memory stay on the
Mac. This first phase is read-only: approvals and questions are still answered on
the Mac. The design and the later phases are in `specs/phone-companion.md`.

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

## How it is protected

- Nothing listens on a network address: only Tailscale reaches the desktop, and
  only devices in your tailnet reach Tailscale.
- Each phone makes its own signing key, which cannot be read out of the phone's
  storage. The Mac keeps the public key, and every sign-in signs a one-time
  challenge; there is no shared password. A pairing code works once, lasts five
  minutes and is withdrawn after five wrong tries.
- Settings lists paired phones. 移除 ends that phone's connection at once and
  stops it from signing in again; it has to be paired anew.
- Turning off 允许配对的手机连接 stops the gateway. The desktop app must be running
  for the phone to connect; the tray icon keeps it running without a window.
