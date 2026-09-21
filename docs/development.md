# Development, testing and maintenance

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/development.md)

Run these commands from the source checkout. Keep individual test reports in ignored local storage.

Pytest isolates inherited Astra installation, workspace and model settings, so
launching it through an agent uses the same fixture configuration as a clean
shell. Tests can still set their own environment with `monkeypatch`. Explicit
live acceptance may opt into inherited settings with the
`allow_application_environment` marker; ordinary tests should use temporary state.

[Reproducible development and maintenance setup](#reproducible-development-and-maintenance-setup) · [Maintenance entry points](#maintenance-entry-points) · [Deterministic replay evaluations](#deterministic-replay-evaluations) · [Verify](#verify)

## Reproducible development and maintenance setup

`uv.lock` and the lockfiles in `ui-core`, `ui-tui` and `ui-gui` are committed. With Python 3.11+, Node
18+ and [uv](https://docs.astral.sh/uv/) installed, run from the repository root
on any supported platform:

```text
uv sync --locked --python 3.11 --extra dev --extra mcp --extra tracing --extra server --extra notebook
npm --prefix ui-core ci
npm --prefix ui-core run build
npm --prefix ui-tui ci
```

The development extra pins pytest, Ruff and Pyright. Add `--extra embedding`
when the local embedding backend is needed; model downloads and native helper
installation are separate. On an existing environment, `uv sync --inexact`
preserves additional installed packages. The shared `astra setup` command uses
`uv sync --locked --inexact`, records enabled extras and dependency freshness,
and runs these locked installation steps plus the interface build. Dependency
conflict checks still apply. The manual commands above remain useful for
development and release checks; use `astra setup` afterward to record the
verified environment.

For desktop development use Node.js 22.12+ and `astra setup --gui --extra dev`.
See [desktop checks](gui.md#development-checks) for the separate GUI acceptance suite.
The normal release gate builds and tests `ui-core` before the TUI; GUI checks remain optional.

The Python wheel contains the Agent, bundled model profiles and Session Recall.
The Ink and Electron UIs, repository-local skills and macOS native helper require the source
checkout and their respective setup steps.

## Maintenance entry points

Windows wrappers remain supported alongside their POSIX counterparts:

| Operation | Windows | macOS/Linux |
| --- | --- | --- |
| Start Astra | `astra.bat` | `./astra.sh` |
| Migrate legacy state | `.\scripts\astra-migrate.bat` | `./scripts/astra-migrate.sh` |
| Run release gate | `scripts\phase_t_gate.cmd` | `./scripts/phase_t_gate.sh` |
| Build sandbox image | `scripts\build-sandbox-image.ps1` | `./scripts/build-sandbox-image.sh` |

Inside the TUI, `/maintenance preview 30` lists expired generated artifacts;
`/maintenance apply 30` removes eligible files and performs passive SQLite WAL
checkpoints. `/maintenance checkpoint` only checkpoints configured databases.
Nested temporary files are considered only inside explicitly configured
generated artifact directories. Root-level `.astra/*.tmp` files retain the
24-hour stale-file policy. Sessions, memory, skills, tasks and checkpoints are
excluded, including misleading `.tmp` names. Symlinks are not followed; files
changed since planning are skipped. Windows additionally verifies file content,
because its creation timestamp does not detect all rewrites. SQLite migration,
backup and checkpoint connections are closed before replacing or removing files.
This is artifact maintenance, not deletion of conversation or activity history.

## Deterministic replay evaluations

Persona and context invariants can be checked without starting a model server:

```powershell
python -m agent.evals.replay
python -m agent.evals.replay --category context-compression
python -m agent.evals.replay --case legacy-work-session-migrates --json
```

Editable JSONL cases live in `evals/persona_invariants.jsonl`. Every run writes
the replayed session artifacts and a structured `report.json` below
`.astra/evals/runs/`. An editable install also exposes the same runner as
`agent-lab-eval`.

## Verify

Install the locked development environment described above first. The release gate runs
Ruff, Pyright, all TUI tests, TUI typecheck/build and the Python suite. Ruff uses
an explicit correctness-focused rule set in `pyproject.toml`; formatting rules
are not a release requirement. Optional acceptance gates are separate:

```text
uv run --locked --extra dev --extra mcp --extra tracing --extra server --extra notebook python scripts/phase_t_gate.py --wheel-smoke
```

During development, run focused checks locally. Before the final cloud run,
complete the local macOS acceptance gate:

```bash
./scripts/phase_t_gate.sh --keep-going --wheel-smoke --native --context-index-performance
```

This includes the Swift suite and the production-sized lexical Context Index
latency/coverage fixture. The latter disables embedding, isolates the vector
database path, runs separately from ordinary tests and retains its original
thresholds.
`--provider-smoke` explicitly enables real model requests. A green automated
gate does not establish live desktop action effects, Appshot capture permission,
or acceptance against a real provider; those still need targeted manual checks.
For final cross-platform acceptance, run the checks at the final commit on
Ubuntu, macOS and Windows. You can collect results from matching local machines
or start one manual run from **Actions → Maintenance → Run workflow**. Record
the commit, operating system, commands and results for each platform; report
unavailable platforms as not run. If a check fails, fix and verify it locally
before rerunning. The manual Maintenance workflow checks the normal gate and
wheel smoke on all three platforms, with native/performance acceptance on macOS.

The separate [Launcher compatibility workflow](../.github/workflows/launcher.yml)
runs automatically when its listed launcher files change in a push or pull
request, and also supports **Run workflow**. It exercises source discovery,
native command forwarding, real Git updates, process exclusion and recovery on
all three platforms. A passing local macOS run does not establish Windows CMD,
PowerShell or Linux acceptance; verify the target commit's completed jobs.

Private-repository runs consume the account's GitHub Actions allowance. Standard
GitHub-hosted runners are free for public repositories; larger runners are
billed separately. See [Actions billing](https://docs.github.com/en/actions/concepts/billing-and-usage).
When private-repository minutes are exhausted, retain local acceptance results
and leave unavailable platform checks pending. Buying extra minutes is optional.
If GitHub reports failed payments as well as a spending limit, check the account's
billing status before retrying; a blocked job has not executed its tests.
The Maintenance gate uses `--keep-going` to collect
independent check failures in one run and still exits unsuccessfully if any
check fails; local invocations stop at the first failure by default. Native
macOS/POSIX permission tests require those host capabilities; portable rejection
and protocol checks still run on Windows. The sandbox image tests require a
Linux Docker engine. Appshot integration separates Swift dependency/build time
from the bounded end-to-end test run.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m ruff check agent tests scripts
.\.venv\Scripts\python.exe -m pyright agent scripts/phase_t_provider_smoke.py
npm --prefix ui-tui run build
```

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m ruff check agent tests scripts
.venv/bin/python -m pyright agent scripts/phase_t_provider_smoke.py
npm --prefix ui-tui run build
```
