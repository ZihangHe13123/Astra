# Appshot: bring a window into the conversation

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/appshot.md)

On macOS and Windows, a shortcut attaches one window screenshot and available interface text to your draft. You choose when to send it.

[Setup](#setup) · [Capture and send](#capture-and-send) · [Troubleshooting](#troubleshooting) · [Model and attachment limits](#model-and-attachment-limits) · [Verification](#verification)

## Setup

Appshot requires an interactive Astra TUI. On Windows x64, build and install its
native helper with:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build_windows_computer_helper.ps1 -Configuration release -Install
```

Restart Astra and run `/appshot enable`; the shortcut is disabled by default.
The Windows bundle contains its runtime DLLs and uses WGC/UI Automation plus
Windows-specific private storage. It does not enable other Computer Use tools.
See [Windows Appshot setup, tests and limitations](../native/windows-computer-helper/README.md).

On macOS, Appshot requires macOS 14+ and the signed native
helper built with `./scripts/build_macos_computer_helper.sh`. Grant the helper
Screen Recording and Accessibility in macOS Privacy & Security. Recheck these
permissions after rebuilding an ad-hoc signed helper; an older grant may not
apply to its new code identity.

## Capture and send

Use the destination TUI once with actual keyboard input before the first
capture. Merely opening a TUI or querying background status does not make it an
eligible recipient. Then switch to the source app and press **Control+Shift+Z**.
The newest eligible TUI receives a draft placeholder; capture does not send a
message. Add your question and press Enter explicitly. With multiple TUIs,
identical most-recent activity is refused rather than broadcast.

Appshot captures an exact visible window selected by your shortcut. System Settings,
Activity Monitor and dialog windows are not excluded by application or page type.
On macOS, Screen Recording permission is required; Accessibility adds available UI text.
If AX root/tree reading fails, the attachment falls back to the screenshot with
an explicit “仅截图” label and an unavailable-text notice for the model. Without
AX window evidence, capture requires one unique visible window in the foreground
app. Ambiguous windows are refused; there is no full-display fallback or source
activation. Available AX coverage is `reported_ax_subtree`, not a full-page guarantee.
Masked/secure text fields are never expanded into hidden plaintext. Ordinary static
text may omit its optional AX subrole without losing its value. Appshot traverses
up to 64 levels (2,000 nodes / 256 KiB / existing time budget), including offscreen
content exposed by the app. It reports truncation instead of promising a complete
page. Restart all TUIs after updating the native helper and recapture old attachments
to obtain text that was previously omitted.

Commands stay local: `/appshot status`, `/appshot enable`, `/appshot disable`,
and `/appshot shortcut Control+Shift+Z`. Editable and pending attachments share
a four-item cap. Removing an unsent placeholder releases its files. While the
backend is busy, captures can enter the draft, but submitting them returns
`backend_busy` and retains the content. Lost acknowledgments use status lookup;
`/appshot pending status` reconciles a pending submission and
`/appshot pending discard` explicitly discards its retained local segment.
Discard does not cancel work already admitted by the backend. A backend restart
can report `unknown`; it never automatically resends the message.

## Troubleshooting

Common refusals include `no_receiving_session` (use a destination TUI first),
`receiving_session_ambiguous` (use the intended TUI again), `shortcut_conflict`
(choose another chord), `permission_unavailable` (check helper permissions),
`source_window_unavailable` (cannot identify one visible window),
`protected_ui` (the console session is locked or unavailable),
and `attachment_limit_reached` (send or remove existing attachments).
On macOS Apple Terminal, focusing an Astra tab selects it as the Appshot
recipient without typing. A bounded read-only probe checks the front tab's TTY
against the TUI process TTY every 400 ms; switching to the source app preserves
the last selected recipient. Repeated observations and background output never
claim activity. This requires macOS Automation access to Terminal; if unavailable
(or on other terminals), press an arrow key in the intended TUI before capturing.
A background window starting up does not select itself.
Appshot reconnects use exponential backoff and stop after five consecutive
short-lived connections; repeated disconnect notices are coalesced. Input in a
disconnected TUI retries the connection; the paused-retry notice is not
repeated until a connection is established again. A connection must remain
established for ten seconds to reset the retry budget. Without the native
helper installed, the TUI prints no Appshot notice and input does not retry;
install the helper and restart Astra. Resizing or changing display modes does
not recreate the client or discard its recipient activity.
`context_budget_exceeded` and `context_budget_unavailable` retain the draft.
Appshot checks the resolved model context window minus output reserve; the status
bar percentage and ordinary proactive compression use the separate 50% threshold.
The two checks also use different accounting: Appshot budgets the fresh complete
request, while the status bar shows recent usage or an estimate.
`appshot_vision_unavailable` also retains the draft when the selected profile lacks
`vision`; select an image-capable profile before submitting a new Appshot. Existing
Appshot history does not block ordinary text-only follow-ups: AX text remains,
with an explicit notice that pixels are unavailable to this model. Original
images stay in session storage and become visible again on switching back.

## Model and attachment limits

Appshot accepts every profile declaring `vision`, including local and custom
endpoints. Official Qwen and GPT-4o/4.1 endpoints use their existing image bounds;
DeepSeek Vision uses the [published 1,024-token image maximum](https://api-docs.deepseek.com/guides/vision/#token-usage).
All models use Astra's text/schema token estimator with 25% slack; serialized
UTF-8 bytes are used only for transport-size checks, never as text token counts.
Other models' image allowance is at least 4096 tokens, increasing with a 32-pixel
patch grid for large images. Text accounting and generic image allowances are
estimates, not exact billing or guaranteed provider bounds. Model names and
endpoint domains choose image rules, not text accounting or admission rights.
Qwen reserves 16,386 tokens per image using its
[published maximum](https://help.aliyun.com/zh/model-studio/vision), not its default resize cost.
Native PNG bounds are
10 MiB, 1–16384 pixels per dimension and 32 million pixels; Qwen additionally
requires dimensions above 10 pixels and aspect ratio no greater than 200:1.

## Verification

`./scripts/test_appshot_e2e.sh` runs private Swift broker → Node client/draft →
Python admission/media/provider-projection fixtures. It does not register a
real hotkey or capture the desktop. Automated fixture evidence, signed bundle
identity and real-machine acceptance are tracked separately. A fixture pass
does not verify capture or permissions on your machine.
