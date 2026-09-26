# Astra installation, startup and updates

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/launcher-update.md)

[Install](#first-installation) · [Upgrade](#upgrade-an-older-checkout) · [Update](#update-from-a-source-checkout) · [Recover](#recovery-and-repair) · [Data](#data-and-installed-distributions) · [Commands](#command-reference) · [Troubleshoot](#troubleshooting)

This is the pre-release source workflow. It also establishes package-installation
ownership and private data paths, without claiming that a public release bundle
or automatic stable-release download is available.

## First installation

Install Python 3.11+, Node.js 18+ for the TUI (including npm), and Git.
The optional desktop GUI requires **Node.js 22.12.0+**, including subsequent
updates and repairs once the GUI is enabled. Setup/update checks this before
installing dependencies or pausing companion services. If Node is too old,
upgrade to a supported Node.js LTS, open a new terminal (especially on Windows),
check `node --version`, then retry `astra setup --gui --repair`.

Setup uses the
committed Python and Node dependency locks. If uv is missing, setup installs a
private copy; it does not install Python packages globally. On Windows, the
launcher runs in CMD or PowerShell; the agent's [Bash tool](execution.md#minimal-bash-environment)
uses WSL separately.

**Windows — CMD or PowerShell:**

```powershell
git clone https://github.com/ZihangHe13123/Astra.git
cd Astra
.\astra.bat setup --install-command
```

**macOS or Linux:**

```bash
git clone https://github.com/ZihangHe13123/Astra.git
cd Astra
./astra.sh setup --install-command
```

Setup prepares `.venv`, installs and builds the interface, and registers the
`astra` command in your user PATH. It preserves an existing `.env`; otherwise,
it creates one from `.env.example`. Configure your model connection in that file
or use the [model connection commands](usage.md#model-connections) after starting Astra.
Installation checks do not require a model connection or API key.

Open a **new terminal** so PATH is refreshed, change to the project you want to
work on, and run:

```text
astra doctor
astra
```

The current directory becomes the workspace unless `SANDBOX_WORKDIR` explicitly
selects another one. The launcher finds its own installation independently of
your working project. Normal startup checks the environment; it does not fetch
code or install dependencies.

## Upgrade an older checkout

An older installation needs **one manual pull** to obtain the new launcher.
Close Astra sessions and services using that installation first, then run these
commands from its existing source directory.

**Windows — CMD or PowerShell:**

```powershell
git pull --ff-only
.\astra.bat setup --install-command
```

**macOS or Linux:**

```bash
git pull --ff-only
./astra.sh setup --install-command
```

If the pull fails, resolve the reported Git issue before continuing. Existing
configuration, conversations and memory are retained. Open a new terminal and
run `astra doctor`; subsequent updates use `astra update`.

## Daily use

Run `astra` from the project you want to work on. The current directory becomes
the workspace, subject to existing filesystem permissions and explicit
`SANDBOX_WORKDIR` configuration. It does not select the Astra installation.

All source wrappers and the installed `astra` entrypoint route through the same
standard-library command parser. `astra` selects Ink. `astra --cli` selects the
legacy text CLI explicitly; `astra --tui` retains the legacy Textual entry.
`agent-lab` keeps its existing legacy compatibility behavior.

Normal startup checks the recorded environment and starts it. It does not fetch
code or run package installation. After manually changing lockfiles or pulling
code outside the updater, run `astra setup` when dependencies are unverified.
First setup is explicit so an ordinary invocation does not silently install
software or modify the environment.

## Update from a source checkout

```text
astra update --check
astra update
```

The update follows the current branch's configured Git upstream. This supports
the Mac-push / Windows-update development workflow without assuming that every
user should follow an arbitrary branch. A source archive without `.git` cannot
perform a Git update; clone the repository to use this workflow.

`--check` fetches into a temporary Git ref, compares the pinned commit and
removes that ref. It does not replace working files, synchronize dependencies or
restart a session. Git object storage may change. Checks can run while Astra is
active. `--json` provides structured results.

Applying an update requires a fast-forward upstream. Local edits to files
untouched by that upstream change are kept automatically, as are local files
already identical to the incoming content. When both sides change a file,
interactive `astra update` lists the local files and offers **Keep local files**,
**Back up and overwrite local files**, or **Cancel**. Keep is the default.

Keep retains each locally changed file whole; it does not merge separate edits
inside the file. Its original bytes, deletion and executable mode are preserved,
and originally staged and unstaged contents stay separate. Other incoming files
are updated normally. Overwrite replaces all listed locally changed files after
backing them up. A successful keep-local update may still have `dirty: True`.

```text
astra update --keep-local
astra update --overwrite-local
```

These explicit policies are useful for scripts. `--json` does not prompt, and
noninteractive overlapping changes require a policy before mutation. Policy
flags cannot be combined with `--check`, `--repair` or `--recover`. An
already-current healthy checkout retains local edits even if overwrite was
requested; there is no incoming update to apply.

Unrelated untracked/ignored files are retained. A local file, including an ignored
one, that an incoming file would replace is included in the choice. Private
configuration/state and generated environments are excluded from source-file
overwrites. File/directory and submodule conflicts that cannot be preserved
unambiguously require manual resolution before mutation.
Resolve intent-to-add entries and hidden index flags on affected paths explicitly;
the updater does not convert them into ordinary staged files or silently drop them.

There is no automatic stash, reset of local commits, or branch switch. Shared or
symlinked generated environments are not mutated by another worktree.

Close interactive Astra sessions before applying an update. New launcher/backend
processes hold process-lifetime leases; source setup/update uses a separate
installation lock to prevent simultaneous startup or another update. The updater
identifies installed companion services and manages their pause/restart itself.
Unknown processes holding the installation remain blockers; no broad Python/Node
termination is used.

### Companion service lifecycle

Local-file decisions and tooling checks happen before services are paused. The
enabled service set is saved in `services.json`; stop intent is durable before
each native call. After pausing, the updater checks again that the installation
is idle before changing code or generated directories. A cancelled choice,
`--check`, or healthy already-current checkout does not restart services.

| Platform | Automatically managed services | Restored state |
| --- | --- | --- |
| macOS | Browser bridge, activity summarizer and activity sync LaunchAgents whose executable/module/working directory identify this checkout; the stable native activity recorder when this checkout owns the activity pipeline | Previously loaded jobs, with unchanged plists, recording settings, exclusions and pairing data. Continuous jobs must be running; periodic jobs need a loaded schedule. |
| Windows | Opt-in Python activity recorder launched from this installation, including its owned summary child | Graceful stop marker and bounded exit wait, then hidden restart with the same validated arguments. Existing pause state stays in place; a fresh running heartbeat is required. |
| Linux | No companion service is currently installed by Astra | No service-manager operations. |

The macOS service set also includes shared MLX embedding workers launched by this
checkout. Native argument inspection identifies their exact interpreter, module
and runtime directory, including paths containing spaces. The updater waits for
active encoding to finish and uses the authenticated loopback stop endpoint; it
does not kill an arbitrary Python process. Original worker directories are
restored before periodic clients restart. A restored worker must expose its
authenticated control endpoint; model preparation can continue in the background.
Rendezvous tokens and embedding contents never appear in update receipts.

Unloaded/disabled services remain off. A LaunchAgent belonging to another checkout
is not modified. The updater compares the loaded identity and saved definition,
then checks its fingerprint again before restarting; edits made during maintenance
are preserved and reported for inspection. Existing stable native executables
are restarted as installed; source maintenance does not silently rebuild or
re-sign them. Appshot daemons belong to sessions and are started/reconnected on
the next session launch. External model and MCP servers are outside this set.

Successful updates print `services_status: restored` and per-service results.
If the code was installed but a service failed to restart, the result remains
`outcome: applied`, with `services_status: restart_failed`, an actionable message
and a nonzero exit code. Other services still get their restoration attempt.
Run `astra update --recover` to retry the remaining service restoration. This
does not roll back successfully installed code or restart services already
restored by the same operation. JSON stdout remains separate from stderr progress.

The updater runs from a temporary copy outside the checkout on a base Python
interpreter. Windows source batch entrypoints finish in a pre-parsed command
block, so changing a batch file during an update cannot change its continuation.
If a Windows command was invoked from the very virtual environment being
replaced, it directs the user to `astra.bat` instead.

Only affected generated directories are backed up: Python for Python-lock or
package metadata changes, Node dependencies for npm-lock changes, and the build
output for interface changes. Unverified environments and explicit repair run
the full synchronization. Existing extras are recorded; the initial adoption
also detects common extras already present in an older environment. `--inexact`
retains additional installed Python packages, while dependency checks still
reject incompatible combinations.

Every changed environment is preserved before mutation. The updater fast-forwards
to the pinned commit, performs the needed locked synchronization, checks Python
imports and syntax, verifies the interface, and writes a receipt. A successful
`applied` result means that the installation is ready for the **next launch**.
It does not claim that an existing process is already running the new code.

<details>
<summary>One-time service migration for an older updater</summary>

An older installed updater may still report the browser recorder as a blocker.
Closing the TUI does not stop that background service. For that one-time upgrade,
temporarily unload the installed browser service before updating:

```bash
launchctl bootout "gui/$(id -u)/com.astra.activity-browser-bridge"
```

After maintenance or recovery completes, restore the same saved configuration:

```bash
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.astra.activity-browser-bridge.plist"
```

These steps apply only to an already installed browser recorder. They preserve
its configuration and pairing data; other services use their own stop/start
procedures.

</details>

## Reading update results

| Output | Meaning |
| --- | --- |
| `outcome: available` | An upstream change is available; the check has not applied it. |
| `outcome: applied` | The update completed; launch Astra again to use it. |
| `outcome: current` | The checkout already matches the checked upstream. |
| `outcome: cancelled` | The working files were left unchanged. |
| `local_changes: True` or `dirty: True` | Local differences remain; this can coexist with a successful keep-local update. |

## Recovery and repair

```text
astra doctor
astra setup --repair
astra update --repair
astra update --recover
```

- `setup --repair` synchronizes the current source configuration, including
  deliberately edited development metadata.
- `update --repair` repairs the current clean commit without fetching a new one.
- `update --recover` resolves an interrupted setup/update before another launch.

Service restoration follows runtime recovery: after a successful rollback, services
restart against the restored old runtime; if rollback is incomplete they remain
paused. The service journal also covers interruption before a code transaction
exists and after one has committed. When only services need recovery,
`outcome: services_recovered` reports that retry. `astra doctor` exposes
`pending_services` and identifies this recovery requirement.

Failed updates restore the recorded previous source commit, original staged and
unstaged local files, and every generated directory they changed. They do not
restore an old conversation database over new user data. Virtual-environment
snapshots are restored to the original path
on the same machine; they are not portable environments or cross-platform backups.

If another process has edited the checkout or changed HEAD, recovery stops with
an actionable error and retains its journal. It does not force-reset that work.
Likewise, failure to replace a Windows-locked directory keeps the recovery record
instead of reporting success. Inspect the error and close the identified holder,
then retry recovery.

For source installs, control files are under `.astra/launcher/`:

| File/directory | Purpose |
| --- | --- |
| `installation.json` | Installation identity and selected extras |
| `environment.json` | Last verified lockfile fingerprint and runtime versions |
| `pending.json` | Authoritative interrupted-operation journal |
| `services.json` | Original enabled companion services and durable pause/restore intent; retained until restoration completes |
| `transactions/` | Generated directories retained until completion/recovery |
| `receipts/latest.json` | Most recent applied/current/recovered result |
| `instances/` | Live process leases and versions captured at process startup |
| `local-changes/<id>/` | Retained original file contents, index snapshot and manifest for a local-file choice |

Do not delete `pending.json` or `services.json` to bypass recovery. If manual
recovery is necessary, preserve the journals and generated-directory snapshots
before changing anything. A service definition changed during maintenance must
be inspected and reconciled with the recorded definition before automatic retry.

Successful local-file updates retain their private backup and print its path.
The `files/` subdirectory contains original file contents under their relative
source paths; `manifest.json` describes types, permissions and local deletions.
To recover a file after a successful overwrite, inspect that saved copy before
restoring it over any newer work. The saved `index` belongs to transaction
recovery at the original commit; do not copy it into an updated Git checkout.

## Data and installed distributions

| Location | Source checkout | Package-installed distribution |
| --- | --- | --- |
| Program files | The actual source root | The installer-owned package location |
| Private state | `<source>/.astra` | Platform user-data directory |
| Sessions | `<source>/.sessions` | `<user data>/sessions` |
| Provider configuration | `<source>/.env` | `<user data>/.env` |

`ASTRA_HOME` overrides the private-state directory. Existing specific database,
settings and session overrides keep their precedence. Known legacy `.astra/...`
settings in `.env` resolve against the selected data directory. Workspace-owned
files, such as project rules and file checkpoints, remain in their own project.
The source launcher keeps `.env` beside the source checkout.

Default installed-distribution data locations are `%LOCALAPPDATA%\Astra` on
Windows, `~/Library/Application Support/Astra` on macOS, and
`$XDG_DATA_HOME/astra` (normally `~/.local/share/astra`) on Linux. Each installation
has a separate control identity, even when a shared data profile is selected.

The registered source command lives in `%LOCALAPPDATA%\Astra\bin` on Windows
or `~/.local/bin` on POSIX. Setup updates only the user's PATH configuration;
open a new terminal for the change to take effect. A command owned by another
installer is not overwritten.

`astra version` and `astra doctor` distinguish source and package ownership.
Package-owned installations are not modified with Git, uv project sync, or npm.
Upgrade them through their original installer. The current Python wheel lacks
the complete Ink assets and native helper; default startup reports that missing
component rather than entering the legacy CLI silently.

A future formal release can supply bundled resources and a versioned runtime
activation adapter without changing command names or private data ownership.
This work does not publish that release, create a stable-release feed, silently
upgrade external model/MCP servers, or implement in-chat restart scheduling.

## Command reference

Run these commands in your terminal. They are separate from slash commands
inside a conversation; for example, `astra doctor` checks installation health,
while `/doctor` checks the running application's connections.

| Command | Use it to |
| --- | --- |
| `astra` | Start the Ink interface in your working project. |
| `astra version --json` | Inspect the installation, commit, paths and tracked running-instance versions. |
| `astra doctor` | Check local installation health without a model key or network connection. |
| `astra setup` | Prepare or verify the current source dependencies. |
| `astra setup --install-command` | Prepare dependencies and register the user command. |
| `astra setup --command-only` | Register only the command and PATH, leaving dependencies alone. |
| `astra setup --extra notebook` | Enable and remember an optional dependency group; repeat `--extra` for more groups. |
| `astra update --check` | Fetch and compare the configured upstream without applying changes. |
| `astra update` | Apply a source update and verify the installation. |
| `astra update --keep-local` | Keep locally changed files exactly and update other files. |
| `astra update --overwrite-local` | Back up locally changed files, then use incoming versions. |
| `astra setup --repair` | Rebuild dependencies for the current source, including intentional metadata edits. |
| `astra update --repair` | Repair the current clean commit without fetching a newer one. |
| `astra update --recover` | Recover an interrupted setup or update. |
| `astra --cli` | Start the legacy text CLI explicitly. |

`setup`, `update`, `version` and `doctor` accept `--json` for structured results.
For setup without PATH changes, use `astra setup`; to register a command in a
directory you manage, use `astra setup --command-only --bin-dir PATH --no-path`.
Before registration, substitute `.\astra.bat` on Windows or `./astra.sh` on POSIX
for `astra` when running from the source directory.

The source wrappers also accept a `PYTHON` executable override; for example,
`PYTHON=python3.11 ./astra.sh setup` on macOS/Linux. `--setup-only` remains
an alias for setup. Use `astra setup --command-only` to register only the command.
The agent connects to configured model endpoints but does not manage their server
processes.

## Troubleshooting

| What you see | What to do |
| --- | --- |
| `astra` is missing or starts an old copy | Open a new terminal. Run `where.exe astra` in CMD/PowerShell or `command -v astra` on POSIX. Register the intended checkout with its source wrapper and `setup --command-only`. Another installer's command is not overwritten. |
| Dependencies are unverified or missing | Close instances using the installation, then run `astra setup`; use `astra setup --repair` if the environment is damaged. |
| Processes are still using the installation | Close the listed interactive Astra sessions. Recognized companion services are paused and restored automatically; an unknown process still needs to be stopped through its owner. |
| Code updated, but a service did not restart | Run `astra update --recover`. This retries the recorded service restoration without reverting the successfully installed code. |
| Local files need a choice | Run `astra update` in a terminal and select Keep, Overwrite or Cancel; scripts can use `--keep-local` or `--overwrite-local`. |
| A diverged branch blocks an update | Inspect `git status --short` and `git branch -vv` in the source directory and resolve the Git history. Overwrite applies to local files, not local commits. |
| No tracking upstream or no Git history | Configure the intended branch's Git upstream. A downloaded source ZIP needs a Git clone to use `astra update`. |
| Windows reports the updating Python environment is in use | Invoke `.\astra.bat update` from the source directory so maintenance runs outside the environment it replaces. |
| Setup/update was interrupted | Close remaining holders and run `astra update --recover`, then `astra doctor`. Retain `.astra/launcher/pending.json` and its transaction snapshots until recovery completes. |
| A copied or moved checkout reports stale environments | Recreate its generated dependencies and register the command at the new path; see [moving a checkout](#moving-a-checkout-between-platforms). |


## Moving a checkout between platforms

A Windows virtual environment and native Node modules cannot be reused on
macOS (or vice versa). A checkout moved to another absolute path also needs its
environments recreated. Close sessions and services using the checkout, remove
only the generated dependency directories, then rebuild them and register the
command at the new source location:

```powershell
# Moving to Windows
Remove-Item -Recurse -Force -ErrorAction SilentlyContinue .venv, ui-tui\node_modules
.\astra.bat setup --install-command
```

```bash
# Moving to macOS/Linux
rm -rf .venv ui-tui/node_modules
./astra.sh setup --install-command
```

Keep `.env`, `.astra` and `.sessions`: they contain configuration, tasks, memory,
conversations and other user state. Open a new terminal after registration.
Before starting on the new host, inspect `.astra/settings.json`,
`.astra/filesystem.json`, and `.astra/models.yaml` and replace any absolute path
that belongs to the old platform. Legacy `.agent_system` data can be merged with
`.\scripts\astra-migrate.bat` on Windows or `./scripts/astra-migrate.sh` on macOS/Linux.

## Acceptance boundary

Automated coverage includes isolated management commands without site packages,
native command forwarding tests per host, real temporary Git remotes, dirty and
divergent repositories, file collisions, update failure and interruption,
state preservation, process leases, and installed-wheel import isolation.
The existing maintenance gate additionally runs Python/TUI tests, typing, lint,
the interface build and optional wheel smoke on its supported OS matrix.

Tests executed on macOS do not count as native Windows acceptance. Windows CMD
and PowerShell, platform-specific helper behavior, and real provider interactions
must be reported separately from the portable automated checks.
