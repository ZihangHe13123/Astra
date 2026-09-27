# Browser interaction: CDP and ordinary Edge

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/browser-interaction.md)

The CDP backend returns structured page observations and supports
`ref:<id>` targets from the latest snapshot. Existing unique CSS selectors remain
supported. Restart Astra to load the updated tools. Safari control is not part of
this Chromium integration.

<a id="everyday-browser-setup-macos-edge"></a>

## Everyday browser setup (Windows and macOS)

The independent `browser-control-extension` connects your existing Edge through
Native Messaging. Keep the existing **Astra Activity URLs** recorder extension;
it cannot operate pages and its permissions have not changed.

1. In Edge's extension management page, load `browser-control-extension` from
   the stable Astra repository as an unpacked extension. Copy its extension ID.
2. From the stable checkout run (replace the ID with the actual 32-letter ID):

   ```sh
   astra browser-control install --browser edge --extension-id YOUR_EXTENSION_ID
   ```

   Chrome uses `--browser chrome`. The installer registers only that exact ID,
   and records the current checkout and Python path. Use explicit `repair` after
   moving the checkout/environment. Temporary worktrees and incomplete package
   installations are rejected. Windows registers only the selected browser's
   current-user HKCU NativeMessagingHosts key; no administrator rights are needed.
   Files under `%LOCALAPPDATA%\Astra\browser-control-host` and the runtime endpoint
   use owner/SYSTEM-only protected ACLs. macOS keeps its existing manifest locations.
3. Restart Astra after updating Python files. With an installer-owned native host
   matching this checkout, the first extension browser task (including tab
   discovery) acquires the endpoint and starts the listener. Chat startup only
   validates setup; it does not claim the endpoint, launch Edge or wait for it.
4. Reload **Astra Browser Control** after updating extension files. Version 0.2.0
   adds storage/alarms and the readiness protocol; allow the requested extension
   permission update. Enable **Auto-connect** once in its popup.
5. Grant website access once. **Allow current tab** authorizes that exact live
   tab. **Allow HTTP(S) sites for new agent tabs** optionally grants access for
   future agent-created tabs, without exposing all existing tabs.
6. Give Astra a browser task directly. `browser_open` connects when needed and
   opens a visible agent tab without a preparatory `browser_connect` call. If
   Edge is closed, Astra launches the ordinary app, without a debugging profile.

Connection waiting is cancellable and bounded to 45 seconds. The extension uses
short retries and a 30-second alarm fallback; browser suspension can delay it.
It reports ready only after the native host authenticates and session grants are
restored. A failed or cancelled connection sends no browser action. New-tab
navigation then has its own 10-second readiness deadline. Cross-origin redirects
require renewed access; opening does not silently grant the destination.

In automatic mode, restarting Astra or a brief disconnect preserves authorized
**live tabs in the same browser session**, after checking exact IDs/origins and
current site permissions. Paused tabs stay paused. Old logical handles and refs
remain invalid: Astra can list and attach surviving granted tabs and take a fresh
snapshot without another popup grant. Interrupted writes are never replayed.
Browser restart or extension reload clears session grants; restored existing user
tabs require selection again. Site permissions persist for new agent-created tabs.

**Stop and revoke all tabs** turns off automatic connection, cancels retries, and
clears saved/live grants. It stays stopped across restarts. Turning off the
Auto-connect checkbox alone disables future retries and saved grants while
allowing an already connected manual session to continue.

Manual connection remains available. Set `ASTRA_BROWSER_TRANSPORT=cdp` (or
`manual`) to keep the legacy runtime startup/default behavior. Set it to `auto`
(or `extension`) to require auto mode explicitly; a missing or mismatched host
then produces a setup error. `ASTRA_BROWSER_APP=edge` or `chrome` chooses the
installed app; if both hosts are installed, Edge is preferred. Auto mode never
silently switches to CDP after a connection failure, and existing bound CDP tabs
retain their targets. Safari and Linux native-host installation are not included.

### Check and repair the optional integration

On Windows, run host-management commands from your own ordinary CMD/PowerShell
window. An isolated agent or packaged process can read back registry writes that
Edge cannot see. The installer refuses known isolated/packaged environments;
do not disable that check or switch to a machine-wide registration to work around it.
An earlier attempt in such an environment needs an explicit `repair` from the
ordinary terminal (or `install` with the exact extension ID if no owned files exist).

```text
astra browser-control status --browser edge --json
astra browser-control repair --browser edge
```

Status and `astra doctor` do not start a host or browser. `configured` means the
registration is valid, not that the extension is connected or any tab is granted;
use `/doctor browser` or `/browser status` inside Astra for live readiness. A native
socket alone is not ready until extension grant restoration finishes. The popup
shows the native connection error as well as its retry state.

Repair/uninstall verify ownership and refuse foreign registrations, edited files,
reparse points and hard links. Repair can tighten an older Windows installation's
ACLs, but refuses a live endpoint: first run `/browser stop` in its owning Astra
session. Never delete its lock to take over. Normal failures restore previously
owned files/registration; unknown artifacts require manual investigation.

Windows uses a Python `.cmd` launcher, binary stdio and a fixed module entrypoint;
no browser argument is interpolated into a command. A native-host directory
containing `%` or a newline is rejected before installation. If your organization
blocks command launchers or Native Messaging, consult its policy administrator;
Astra does not weaken those policies or silently switch transport.

For a real-browser smoke test, stop the current Astra browser session, run
`python scripts/browser_control_smoke.py` with the checkout's virtualenv Python,
and follow its prompts on the disposable loopback page. Grant **only that tab**.
The script checks read/fill/click, runtime restart/reconnect without replay, and
manual revocation. It never requests private pages or broad site access.

## Model workflow

The repository skill
[`visible-browser-cdp`](../.astra/skills/operations/visible-browser-cdp/SKILL.md)
retains its legacy name but covers extension auto-connect, website/tab grants,
serial form interactions, uncertain outcomes, and the explicit CDP fallback.
Load its current contents with `skill_view` instead of reusing older CDP-only
instructions from a conversation.

When an interactive tool requires approval in the Astra terminal, tell the user
once before the first action that returning there to approve is expected. That
focus change is separate from browser targeting. Extension DOM operations use the
bound tab; native foreground actions use the supported Computer Use takeover
flow. A later screenshot with Terminal in front does not establish where an
earlier click went. Verify the tool result and target state, rather than activating
Edge or sending global keys to compensate for approval.

- Read `browser_snapshot(refresh=true)` to observe the current bound page without
  navigating or reloading it. The returned `elements` contain role, accessible
  name, disabled state and refs. Use the exact latest `ref:<id>` as selector.
- For choice questions or mixed forms, use `scope="form", include_text=false`.
  `groups` retains question prompts; each element's `group` identifies a group's
  `id`. Group `ref` values can be passed to `browser_read` for more context.
  Follow `nextOffset` when present, and respect `nameTruncated` instead of
  guessing omitted text. Use `scope="editable"` for text editors.
- `browser_check` sets up to 20 checked-state goals, skips already satisfied
  targets and verifies the final DOM states. With extension 0.3.3, capability
  negotiation distinguishes the running controller from the injected page.
  New page helpers can service the same goals through an explicitly authorized
  click route when a legacy controller lacks native check. This is automatic;
  the model keeps using `browser_check`. Snapshots expose `checkRoute` and
  `nativeCheck`. A definite `unsupported_operation` is not an expired selector:
  follow its recovery hint rather than changing refs/CSS and retrying.
- Click/type/select validate the target before dispatch. Their result includes
  an observation status and, when available, `after`, the new snapshot. Use refs
  from that `after` directly; an extra refresh intentionally invalidates them.
- `observed` means a page/control change was observed, not that the larger task
  succeeded. Check the expected confirmation, value or navigation yourself.
- `no_observed_change` and `unknown_outcome` are not success. Do not automatically
  retry writes: inspect the page and decide from the actual state.
- A click's immediate observation can precede a slow navigation. Use a fresh
  snapshot or a bounded `browser_wait` to check the result; do not submit again
  just because the immediate snapshot still shows the form.
- Snapshot, wait, tab discovery, status, and connect results bypass the ReAct
  turn cache. The four observation tools allow repeated reads up to 20 calls
  each per turn (or per code-mode program). Connect and write tools retain their
  repeated-call guards. This prevents cached refs from being presented as a live
  refresh without allowing unbounded observation loops or automatic write replay.
- `browser_wait` can wait for an element, text or URL substring. A timeout is an
  unmet condition, not a successful wait. Use observed or explicitly known
  conditions; multiple conditions must all match. Extension receipts report
  the requested and actual timeout (up to 10 seconds), conditions and whether
  the URL changed. Inspect the returned page for the result before waiting again.
- Origin changes require renewed access. A stale, replaced or detached element
  requires a fresh snapshot; the system does not retarget a different element.
- Handoff pauses the same extension tab; resume is explicit. Closing an attached
  user tab detaches it, while closing an agent-created tab closes that tab.

## Multiple Astra instances

Ordinary startup and status checks do not acquire the browser endpoint. The
first browser operation enters the serialized readiness/connection path.
The extension endpoint has one runtime owner at a time. When another Astra window
holds it, opening, connecting or listing tabs here files a release request and waits a
few seconds. The owning window hands control over by itself once it has no turn and no
browser operation running, and shows "Browser control handed to Astra window ...". Its
logical tabs and refs expire; it reconnects the next time it needs the browser. There is
no idle timeout: an idle owner keeps its tabs until another window asks.

If the owner stays busy, `already owned` names that window and reports an advisory
owner PID; verify the process, start time, terminal and listener before identifying
the instance. A quiet CPU sample does not mean its session can be discarded.
Continue in the owning instance, or run `/browser stop` there to release control
without exiting Astra. `/browser status` inspects without acquiring the endpoint and
never asks for a handover.
Switching or resetting conversations also releases old connections and logical
handles; persisted rows remain history only. Release drains dispatched operations.
If cleanup fails, new work stays blocked until `/browser stop` succeeds. Disconnecting the extension does not release the process lock;
do not delete `owner.lock` or terminate another session to seize control. Diagnostic
output should select only necessary PID/port fields, never dump `endpoint.json`
with its connection token. A browser debugger banner does not identify this lock's
owner.

After the owner releases control or exits, retry tab discovery and bind with fresh handles. A successful
empty list means no authorized tabs are discoverable, not that the lock is still
held. For an existing user page, restore **Allow current tab** and attach it; do not
silently open a substitute form. A pre-attachment failure cannot be recovered with
snapshot/read on an unbound tab, even if a generic recovery hint suggests them.

## Current limits

Page snapshots are bounded to 150 interactive elements, 12,000 text characters
and a total serialization budget below 64 KiB. Password and hidden-text inputs
are excluded. File inputs expose metadata only, including when visually hidden;
arbitrary page text may still contain sensitive information. Open
shadow roots and accessible same-origin child frames are supported. Cross-origin
or opaque sandbox frames and closed shadow roots remain limitations rather than
being silently presented as fully inspected.

The form scope uses a smaller 10K-character observation budget and compact JSON
through sanitization/persistence. Larger forms paginate; prompts are bounded to
2,000 characters per group and option names to 1,000, with truncation flags.
These are DOM observations. Checked-state verification does not establish
application saving or server persistence.

The extension explicitly does not take screenshots: Chromium's visible-tab
capture cannot atomically target a specified tab. CDP screenshot support remains.
The extension exposes a fixed operation list, not arbitrary eval, cookies or a
shell. The native host authenticates a user-private localhost endpoint; it never
sends that token to the browser. At most one Astra runtime owns this endpoint.

Uninstall the native host with:

```sh
astra browser-control uninstall --browser edge
```

Then remove the control extension in Edge. This does not uninstall Activity URLs
or change existing activity history.

## File selection without desktop focus

With Browser Control **0.4.1**, `browser_upload` selects, replaces or clears files
in an already authorized tab. Reload the updated extension and restart Astra.
No new extension permissions are required. Existing user tabs need a new
**Allow current tab** grant after extension reload.

1. Call `browser_snapshot(tab_id=..., scope="form", role_filter="file",
   include_text=false)` and identify the exact file input and its fresh ref.
   Hidden native inputs and accessible same-origin frames are supported.
2. Call `browser_upload(tab_id=..., selector="ref:...", paths=["/absolute/file"] )`.
   Approval names the exact local files and website. A general website write
   grant does not permit reading arbitrary files. A website may upload as soon
   as selection changes, so this is a file disclosure operation.
3. Inspect `verified`, `files` and `after`. Verification reads `input.files`
   back and compares filename, size, MIME type and order. It does not establish
   server receipt or submission. A final Submit action is separate.

Use `paths=[]` to clear the input. Multiple files require its `multiple` flag.
If clearing rebuilds the input, read-only verification can confirm the empty
replacement with `verification_source: replacement_input`, provided the original
document, grant, form and unique field identity still match. It sends no further
input; ambiguous or revoked targets remain unconfirmed.
Limits are 10 ordinary files, 32 MiB per file and 64 MiB total. Directory inputs,
cross-origin frames, closed shadow roots and drop-only widgets are unsupported.
`accept` mismatches are rejected before selection; matching that hint does not
prove the server will accept the file. CDP and old extension controllers return
`unsupported_operation` before file content is read or transferred.

Transfers use bounded chunks through Native Messaging and never open a file
picker, activate the app, click the input or submit the form. Navigation,
revocation, handoff, target replacement and transfer expiry invalidate pending
work. An uncertain commit is not replayed: inspect current file metadata before
continuing. `browser_fill` and `browser_type` still reject file inputs.

## Form editing and verification

Forms should start with `browser_snapshot(scope="editable")`. The live snapshot
includes the accessible same-origin frame tree (including nested about:blank and
srcdoc documents), element frameRef, name/context, editable/readonly flags and
current value. `role_filter`, `frame_ref`, `offset` and `limit` narrow observations
before the element budget is applied. Normal snapshots prioritize editable fields.
A frame reference changes with its document/root/URL generation; element refs also
expire on resnapshot, replacement, removal, navigation or grant revocation.

Use `browser_fill(selector="ref:<returned ref>", text="complete value")`, then
use the returned `after` refs for the next field. `browser_read` reads exactly one
target without invalidating refs. A CSS selector must uniquely match across all
accessible frames; there is no first-match fallback. `browser_type` retains its
replace semantics and now also verifies the target. Operations serialize per tab,
including observation; queued stale refs fail before mutation rather than being
resolved to another field. Different tabs remain independent.

Native input setters dispatch input/change. Contenteditable insertion uses the
selected element's own document selection and native editing command with escaped
plain text and explicit line breaks; it does not use TinyMCE.activeEditor, global
paste, OS keyboard input, browser activation or debugger focus emulation. The
page can require internal element focus even when the tab and Edge are background.
A successful `verified` result means the target value matched after the write,
not that a server saved it. Always check the app's save state or response separately.
Unknown dispatch, stale or ambiguous targets and verification failures are
structured tool errors with no automatic replay. Cross-origin and opaque sandbox
frames remain excluded. No new extension permissions are introduced.

Run these automated gates from the repository root. The commands name the test
files explicitly; `node --test browser-control-extension/tests/` is not an
equivalent invocation on the runtime used here.

```sh
npm install --prefix /tmp/astra-browser-test-deps jsdom@26.1.0 tinymce@6.8.6
NODE_PATH=/tmp/astra-browser-test-deps/node_modules node --test \
  tests/test_browser_page.cjs \
  browser-control-extension/tests/control.test.mjs \
  browser-control-extension/tests/worker.test.mjs
.venv/bin/python -m pytest -q \
  tests/test_browser_auto_connect.py \
  tests/test_browser_backend_router.py \
  tests/test_browser_control_installer.py \
  tests/test_browser_control_transport.py \
  tests/test_browser_fallback.py \
  tests/test_browser_interaction_tools.py \
  tests/test_browser_page.py \
  tests/test_browser_regression.py \
  tests/test_browser_session.py \
  tests/test_browser_tools.py \
  tests/test_cdp_backend.py \
  tests/test_extension_browser_backend.py \
  tests/test_tool_approval_scopes.py \
  tests/test_dynamic_tool_registry.py
```

Investigate failures and explain changes in coverage when updating these gates.

To reproduce the independent renderer acceptance, use a fresh output filename
for each run so old progress or report files cannot masquerade as new results:

```sh
python3 scripts/browser_frame_acceptance.py \
  --vendor-dir /tmp/astra-browser-test-deps/node_modules/tinymce \
  --output /tmp/astra-frame-acceptance-run-1.json
```

Open the printed loopback URL in Edge, start the test, select another tab and
keep another app in the foreground. The fixture checks four equally named
TinyMCE iframe editors for 20 rounds, including multiline text, target readback,
all other values unchanged, editor model and hidden form synchronization, plus
hidden/unfocused state after every fill. It tests the real shared page helper;
installed extension/native messaging acceptance is a separate gate. Detach any
other debugger that emulates focus before measuring background behavior.

Check the current run's `.progress.json` if the fixture is waiting. It requires
both `visibilityState === "hidden"` and `hasFocus() === false`. Returning to the
terminal for approval can remove document focus while leaving the selected browser
tab visible. Select another observed browser tab as the fixture instructs, then
leave another app in front; do not repeatedly click Start or sleep while ignoring
the unmet predicate. Missing progress alone does not prove a click was never
dispatched. Use fresh observations and respect uncertain outcomes. Stop only the
test server started for this run, not every process matching the script name.

## Choice states and compact observations

Snapshot elements, read targets and action `after` observations expose live
checkbox/radio `checked` independently of form `value`. Native inputs report a
boolean; native checkboxes additionally report boolean `indeterminate`. ARIA
checkboxes report true, false, `"mixed"` or null, while ARIA radios report true,
false or null. Null means an absent/invalid attribute; non-choice elements omit
these fields. Attribute-only ARIA changes also count as observed changes. A state
observation is not a server-save confirmation, and click outcomes retain their
existing meaning and no-replay rules.

Once the page's text has been read, request
`browser_snapshot(scope="editable", include_text=false)` or combine the option
with a role/frame filter. This refreshes the scoped observation and returns
`text=""`, `textIncluded=false`; values, checked state, frame context, new refs,
pagination and limitations remain available. Subsequent fill/click/select `after`
observations retain that setting. If the selected frame disappears, the fallback
observes the whole page and retains the text setting. Invalidation clears the
setting, and different pages remain independent.

`include_text=true` is the compatible default. Requesting text after a compact
cached result obtains a fresh full-text observation, rather than returning the
empty stored text. Use it for new page content or the final save indicator;
existing conditional waits still inspect page text. A compact response reduces
repeated content but does not remove origin checks, reference validation, target
readback or per-tab serialization.

The same local fixture server also serves
`/tests/fixtures/browser-observation-acceptance.html`. Open that URL in an isolated
browser and click **Run observation acceptance** to check two real TinyMCE
editors, native and ARIA choice states, compact after observations, stale refs,
text restoration and a representative payload reduction of at least 40%. Its
report is still renderer evidence, separate from the installed native messaging
path; it does not measure background visibility/focus predicates.

## Checked choice batches

`browser_check(selector="ref:<fresh>", checked=true)` sets a unique radio or checkbox. For independent choices use `checks=[{"selector":"ref:<fresh>","checked":true}, ...]` (1–20). The shared page helper supports both extension and CDP. Preflight rejects ambiguous, disabled, unsupported, stale or conflicting native-radio targets before any click. Each changed goal receives at most one click, with bounded state observation; successful earlier goals are never replayed after a later failure. The response includes verified, completed, failedIndex, clickCount, per-goal results, dispatch_state, verification_state and one compact after snapshot. Already satisfied goals produce zero clicks. A checked result does not prove application save or submission.

Page snapshots advertise `capabilities.check` and `checkBatchLimit`. Existing click/type/select APIs remain available. The transport treats check as a write and reports uncertain disconnect/timeout without replay.

## Updating and verifying

After updating, restart the Astra instance and reload its control extension as
needed. Verify the loaded extension version separately from the source manifest;
reloading can invalidate existing tab grants. Observe the returned capabilities
instead of assuming every installed version supports the same operations.

The automated gates and renderer fixtures above cover protocol, reference and
form behavior. A fresh installed-extension/native-host run checks a different
path. Record exact versions and channel-specific results under `output/`; fixture
success and DOM readback alone do not establish that a real application saved or
submitted the data.
