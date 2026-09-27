# Astra Browser Control (opt-in)

This MV3 extension is independent of `browser-extension` (Activity URLs). It does
not record browsing, expand the recorder's permissions, or expose a network,
cookie, arbitrary JavaScript, or shell proxy. For Windows/macOS auto-connect setup
and acceptance evidence, see [browser interaction](../docs/browser-interaction.md).
CDP remains an explicit alternative.

## Reviewable manual setup

1. Load this directory as an unpacked extension from Edge or Chrome's extension
   management page with developer mode enabled. Browser internal pages and
   extension installation must be handled manually by the user.
2. Copy the actual extension ID. Register the separate native messaging host
   `com.astra.browser_control` with Astra's native-host installer, supplying that
   exact ID. Start Astra's extension transport runtime before connecting.

   ```text
   astra browser-control install --browser edge --extension-id YOUR_EXTENSION_ID
   astra browser-control status --browser edge
   ```

   Use `--browser chrome` only when intentionally binding Chrome. Installation is
   separate from `astra setup`: setup does not discover IDs or grant browser access.
   Windows writes only HKCU for the selected browser and uses a private Python
   command launcher; macOS retains its shell host. Use a stable source checkout.
   On Windows run these commands in an ordinary standalone CMD/PowerShell, not
   an isolated agent shell whose registry may be invisible to the browser.
3. Open this extension's popup and press **Connect to Astra**. On a regular
   HTTP(S) page, press **Allow current tab** and accept that site's permission
   prompt. A native host connection alone grants no tabs.
4. Use Astra's `browser_tabs` to see granted IDs, then connect with
   `transport="extension"` and `target_tab_id` set to the chosen real ID. With
   exactly one grant, attach may omit the ID.
5. For agent-created tabs on other sites, optionally click **Allow HTTP(S) sites
   for new agent tabs**. This explicitly requests optional HTTP(S) host
   permissions; it never grants existing tabs automatically.
6. **Stop and revoke all tabs** disconnects the host and clears all grants.
   With auto-connect enabled, a brief host disconnect can restore exact live-tab
   grants in the same browser session after revalidation. Browser restart or
   extension reload requires fresh grants. No queued action is replayed. Granted
   host permissions may remain installed, but do not authorize existing tabs by
   themselves. Remove these permissions through the browser's extension settings.

The popup preserves native connection errors while retrying. Registration status
is not proof of connection: only the ready handshake enables **Allow current tab**.
For a moved checkout or repairable owned installation, first run `/browser stop`
in the owning Astra session, then `astra browser-control repair --browser edge`.
To remove only this host, use `astra browser-control uninstall --browser edge`;
remove the extension separately. Neither action touches Activity URLs. See the
[integration diagnostics](../docs/browser-interaction.md#check-and-repair-the-optional-integration)
for ACL, policy and source-installation boundaries and the manual smoke test.

## Capabilities and boundaries

- Allowed operations: tabs, attach, open, snapshot, click, type, fill, check, read, select, wait,
  upload_prepare/upload_chunk/upload_commit/upload_abort, screenshot (explicitly
  unsupported), handoff, resume, close.
- Version 0.4.0 adds `browser_upload` without new permissions. It discovers even
  hidden native file inputs, transfers authorized files in 256 KiB chunks, sets
  `input.files` in the exact document and verifies file metadata. It never opens
  a picker, activates a window or submits a form. Sites can upload on change.
  Pending transfers bind the tab grant, origin, frame and element; they expire
  after 60 seconds idle or five minutes total, and are discarded on navigation,
  stop, disconnect, handoff or regrant. Only commit is a page write, with no
  automatic replay after an uncertain result. See the [file workflow](../docs/browser-interaction.md#file-selection-without-desktop-focus).
- Version 0.4.1 verifies an empty replacement input after clearing when the same
  document, grant, form and unique field identity are retained. This is read-only
  result verification, with no new input or authority for the replacement node.
- In 0.3.3, the worker announces its actual controller operations before the
  unchanged protocol-1 ready frame. Astra combines those capabilities with the
  page helper's capabilities, rather than assuming page support means the
  controller can dispatch the operation. Legacy controllers remain supported:
  when the updated page helper advertises `checkViaClick`, `browser_check` uses
  an explicit checked-goals payload through the authorized `click` operation.
  Both routes use the same validation and verification engine. No fallback is
  attempted after a partial, timed-out, disconnected or unknown write.
- `check` sets one or up to 20 independent checked-state goals in a
  single request, skips satisfied targets, verifies final states, and returns
  one compact observation. Restart Astra after updating Python; reload this
  extension to activate the new controller and its native `check` route. An
  older controller can use the compatibility route only when it loads the new
  page helper. Snapshots report `checkRoute` and `nativeCheck`; unsupported
  operations report that no input was dispatched, with a specific recovery hint.
- `browser_snapshot(scope="form", include_text=false)` groups form controls by
  question/fieldset, retains bounded prompts in `groups`, and exposes option
  states and fresh refs in `elements`. It omits navigation controls and limits
  observations to about 10K characters, with `nextOffset` pagination. Group refs
  are readable if a prompt needs closer inspection. Standard snapshots retain
  their existing shape.
- In 0.3.1, snapshot/read and action `after` expose checkbox/radio `checked` state.
  Native checkboxes also expose `indeterminate`; ARIA mixed/unknown states are
  preserved instead of inferred from the form value. Snapshot `include_text:false`
  omits full-page text (`textIncluded:false`) while retaining values, state and
  fresh refs. Subsequent action observations retain that option; request
  `include_text:true` to restore text. The default remains true.
- Only granted tabs and session-created HTTP(S) tabs are exposed. Incognito and
  internal browser pages are rejected. Navigation binds permission to origin;
  cross-origin changes revoke the tab grant. Same-origin navigation invalidates
  references. Detach/regrant and worker restarts also invalidate references.
- Page code runs in the isolated world. Snapshot references use `ref:<id>` in the
  selector field. Metadata-only snapshots check live URL without invalidating
  references. Writes require the approved `expectedOrigin` on the wire and check it against
  the current grant and live page. Grant replacement cancels an older request.
  Writes dispatch once; rejected/missing responses or disconnection after
  submission return `unknown_outcome`, so inspect afresh before deciding what to
  do next. Validation failure before submission remains an ordinary error.
- Handoff pauses writes to the same bound tab; resume is explicit. Close detaches
  user tabs and closes only session-created tabs. Revoked or disconnected owned
  tabs remain open and need a new explicit grant (ownership is not persisted).
- Screenshot fails closed: Chromium `captureVisibleTab` cannot atomically bind
  capture to a tab ID. Even before/after focus checks leave a switch race, so this
  extension never invokes it. Use CDP for screenshots.
- Wire messages use unique string IDs and at most 1 MiB JSON. The browser/native
  host supplies Native Messaging length framing. Duplicate IDs are rejected,
  including in-flight duplicates. IDs remain suppressed across explicit native
  reconnects within the worker lifetime. After 100,000 IDs the worker fails
  closed until restarted; Astra must use fresh IDs across worker sessions.
- Wait accepts `timeoutMs` from 0 to 10,000 and polls supplied selector/ref, text
  and URL substring conditions together. Probes preserve existing references; a
  fresh snapshot follows success or timeout. Unmet conditions return `timeout`;
  expired references return `stale_snapshot`. With no conditions it waits the
  requested duration before observing. Observing a condition does not establish
  that a larger application task completed.
- Native ports keep modern Chromium workers alive, but an OS/browser shutdown
  revokes access. No browser installation or live acceptance is implied by tests.

## Local checks

```sh
node --test browser-control-extension/tests/control.test.mjs browser-control-extension/tests/worker.test.mjs
```

Mocked Chrome API tests cover grants, origin changes, stop/disconnect, private
pages, ownership, duplicate dispatch suppression, handoff/resume, popup sender
validation, request bounds and fail-closed screenshot behavior. Page-helper
DOM fixtures are tested separately. Actual browser permission prompts, native
host launch and live application effects still need real-machine acceptance.

With jsdom available through Node's normal module lookup (or `NODE_PATH`), run
`node --test tests/test_browser_page.cjs tests/test_browser_compatibility.cjs`
from the repository root. The compatibility suite includes the exact historical
0.3.0 controller fixture, long prompts, pagination, iframe identities, checkbox
idempotence and unknown-write handling. Registered-tool acceptance against an
isolated real CDP browser is available through
`scripts/cu_form_browser_acceptance.py --scope form`; start the local server
with `scripts/cu_form_fixture.py` first. This does not replace live extension
permission/native-host acceptance.

With Playwright available, the opt-in test below launches an isolated headless
Chromium instance, verifies more than 1 MiB of file bytes against SHA-256 and
exercises hidden/multiple/iframe inputs, clearing and transaction failures:

```sh
ASTRA_BROWSER_UPLOAD_E2E=1 ASTRA_BROWSER_EXECUTABLE=/path/to/chromium \
  node --test tests/test_browser_upload_e2e.cjs
```

This validates the real page engine. Installed-extension Native Messaging and
the target website require separate acceptance; this test never submits a form.
