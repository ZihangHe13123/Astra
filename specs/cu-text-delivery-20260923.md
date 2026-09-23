# CU text delivery into browsers and through autocomplete

Follow-up to `specs/computer-use-robustness-20260922.md` (C.1–C.3) after live Edge 153
acceptance on 2026-09-23.

## Evidence

- **ABC layout, Edge new-tab search field.** The helper logged
  `TYPE-ROUTE safety=safeASCIIKeyboardLayout preflight=settable` and planned
  `background_ax_only`. The AXSelectedText write returned success and the receipt
  acknowledged it. The field, its AX value and two later observations all stayed empty.
  WebKit rejects the same write, so its fields already fell back to keyboard; Chromium
  accepts the write and ignores it.
- **Pinyin, Edge omnibox.** Keyboard typing stopped with `stale_snapshot` at
  `before_key_down` once the 1038×159 suggestion window opened, and nothing was logged.
  The overlay continuity rule required the focused field to equal the planned authority
  exactly, geometry included. The ordinary text path already accepts the same element
  moving or resizing during input.
- Act-time refusals such as `input_focus_required` with nothing acknowledged were shown as
  `dispatch_state=unknown` with `next_step=handoff`. The helper reports `unknown_outcome`
  whenever input may have started, so this caused handoffs that were never needed.

## Contract

- **Web content.** A text field under an `AXWebArea` ancestor never takes an AXSelectedText
  write; it is typed by keyboard. The check is structural: a bounded parent walk, no app
  names.
- **Other AXSelectedText writes are read back.** When the value is readable and stays
  unchanged for 300 ms (typing text equal to the current selection excepted), the write
  is `ineffective` and the process is remembered for the helper lifetime.
  - Keyboard plans fall through to keyboard delivery.
  - Background plans report `input_focus_required` with `inputStarted=false`, so the
    retry is routed to keyboard.
  - Unreadable or truncated values are never evidence of an ignored write.
- **Continuing text through an app-owned contained overlay.** Typing may continue when
  the focused element keeps the same identity, role and subrole, is not secure, and its
  current geometry lies inside the validated window. A changed identity, role or
  subrole, a secure field, or geometry outside the window still stops typing. Each
  rejection logs `TEXT-CONTINUITY-REJECT reason=…`, without any text content.
- **Receipts.** A structured result naming a pre-input refusal (`input_focus_required`,
  `stale_snapshot`, `target_not_frontmost`, `target_gone`, `secure_target`,
  `out_of_bounds`), with `last_acknowledged_action == -1` and no uncertain outcome, is
  `not_dispatched`. Missing results, `helper_failed` and `unknown_outcome` stay
  conservative.

## Verification

- TDD in Swift (`AXTextWriteEffectTests`, `KeyboardTextContinuityTests`) and Python
  (`test_computer_feedback.py`).
- Live: `.venv/bin/python -m scripts.cu_live_acceptance` types into a local fixture page
  under ABC and Pinyin, with the page as the independent oracle, then into the omnibox
  while its suggestion window is open.
