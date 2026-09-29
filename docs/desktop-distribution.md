# Standalone desktop distributions

Astra can now be assembled as a relocatable macOS application or Windows application folder. The builder copies the locked Electron distribution, compiled GUI/shared client, a hash-pinned python-build-standalone interpreter, locked backend dependencies and selected backend source/resources. The installed application does not need a checkout, Python, Node, npm or uv on PATH. Build tools are required only on the machine producing the application.

This is a local packaging and recovery foundation. macOS local builds use an ad-hoc signature; Windows builds are unsigned. Developer ID signing, Apple notarization, Windows publisher signing, a public update feed and a graphical installer are separate release gates. The package is not an officially signed public release.

## Build

Build on the same OS and architecture as the target. The checked-in runtime lock supports `darwin-arm64` and `win32-x64` and pins CPython 3.12.14 from python-build-standalone release 20260924 by URL and SHA-256. `uv.lock`, both npm lockfiles and the runtime lock are recorded in the runtime descriptor. uv 0.11.27 is required; Node 22.19+ is required for building the GUI. Runtime dependencies are installed using a hashed export of `uv.lock`, with binary wheels only.

```sh
npm --prefix ui-core ci
npm --prefix ui-gui ci
npm --prefix ui-gui run build
bash scripts/build_macos_computer_helper.sh --output-root /absolute/path/native-build
python scripts/package_desktop.py --target darwin-arm64 \
  --native-helper /absolute/path/native-build/AstraMacComputerHelper.app \
  --output /absolute/path/Astra-desktop-release
```

Use a Python 3.11+ build interpreter. For Windows, use `--target win32-x64`. A matching `AstraWindowsComputerHelper` directory may be supplied with `--native-helper`; otherwise native computer use is explicitly unavailable. The package never searches another checkout or PATH for a helper. The macOS builder verifies the helper signature and recorded source revision before inclusion. A helper build still needs its own native and real-device qualification; bundling does not establish computer-use acceptance.

The output contains `Astra.app` (macOS) or `Astra/` (Windows), `release.json`, `release.sha256` and an updater wrapper. Copy the application to the desired installation directory and launch it normally. Each output path must be new; the builder does not overwrite previous releases.

LibreOffice kit and its target native package remain outside ASAR in `Resources/app/node_modules` or `resources/app/node_modules`, preserving their notices and runtime files. Source-only development dependencies and node_modules directories are not copied wholesale. The backend copy uses Python modules plus a small named resource allowlist; `.env`, `.astra`, session logs, VCS directories and arbitrary JSON files are excluded.

## Runtime and user data

Application resources are immutable. The runtime descriptor records target, interpreter, dependency versions, lock hashes and native helper policy. The release manifest records every file's size, SHA-256 and permissions, plus internal symlinks. Verification rejects missing/extra files, incorrect hashes/modes, path traversal and external/broken symlinks. Full verification occurs after packaging and before and after update staging; startup validates identity and entry points without rehashing the whole app.

User settings, model keys, sessions, logs and Electron data use the normal Astra user directory (`~/Library/Application Support/Astra` on macOS, `%LOCALAPPDATA%/Astra` for Python defaults on Windows). The GUI selects its platform application-data directory. An explicit `ASTRA_HOME` can select a profile outside application resources. Source installations keep their existing `.astra`, `.sessions`, `.env` and updater behavior.

Packaged startup replaces inherited source/interpreter paths, disables user site packages and bytecode writes, and resolves the helper from the bundle. Both the desktop host and backend processes retain the existing installation runtime leases. An idle desktop window therefore prevents an update from replacing its application.

The read-only `load_workspace_dependencies` tool returns the bundle's Python path, library path and exact distribution versions, plus the Office preview engine's availability. It does not install packages. Source installations report that no bundled runtime is available. `python-docx`, `python-pptx`, `openpyxl`, Pillow and pypdf are included through the selected locked runtime dependencies. Structural checks, rendered-page checks and formula recalculation remain separate evidence.

## Explicit updates and recovery

The updater runs from the downloaded candidate release, using that candidate's bundled interpreter. Keep the candidate outside the installed application directory, particularly on Windows where running executables can prevent replacement. No system Python is needed. There is no automatic network feed or automatic interruption of active tasks.

1. Obtain the candidate release and its manifest SHA-256 through a trusted channel. The digest from a file shipped beside an untrusted download only checks consistency; it is not publisher authentication.
2. Stage the candidate, verifying the expected digest, application name, target and complete payload. Staging copies to a sibling of the installed app and verifies the copied bytes again.
3. Close the desktop normally, using the shared task/approval/question quit checks. Apply refuses any remaining runtime leases. It records a recovery journal, renames the original aside, swaps in the staged application and runs backend import/stdlib health checks. Any failed replacement or health check attempts to restore the original. An interrupted replacement blocks new leases until recovery.
4. Launch and verify the updated application. The previous version remains available until explicit finalization. `recover` rolls back it without altering user data; `finalize` deletes only the saved previous application after successful verification.

```sh
/path/to/candidate/astra-desktop-update stage \
  --candidate /path/to/candidate --application /Applications/Astra.app \
  --target darwin-arm64 --manifest-sha256 EXPECTED_SHA256
/path/to/candidate/astra-desktop-update apply --application /Applications/Astra.app
# If needed, close the app and restore the previous application:
/path/to/candidate/astra-desktop-update recover --application /Applications/Astra.app
# Or retain the update and discard its saved previous application:
/path/to/candidate/astra-desktop-update finalize --application /Applications/Astra.app
```

On Windows use `astra-desktop-update.cmd` and the application directory (for example `C:\Apps\Astra`). The caller must have permission to replace the installation directory. No administrator escalation or quarantine bypass is performed. Recovery uses a journal adjacent to the installation so it remains available if an interruption occurs between directory renames.

## Qualification

`tests/test_desktop_distribution.py` covers target/hash/mode validation, external symlinks and traversal, profile separation, active leases, failed health checks, interruption after the first rename, rollback, finalization and backend content allowlisting. `ui-gui/tests/packaged-runtime.test.ts` covers fixed entry points, environment cleanup, source compatibility and bounded lease shutdown.

The manual `desktop-package.yml` workflow builds on macOS and Windows and retains artifacts without publishing them. Windows native computer-use and a real Windows installation/upgrade run require a Windows host. A successful workflow configuration or local Python simulation does not establish those acceptance results.
