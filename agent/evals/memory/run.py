"""Memory evaluation for Astra: seeded synthetic worlds, real prompt assembly.

Offline (default, deterministic, no model or network)::

    python -m agent.evals.memory.run                      # modes off, session and all
    python -m agent.evals.memory.run --mode session --out report.json
    python -m agent.evals.memory.run --scenario dev_env --scenario household --keep-state /tmp/memeval

Each scenario is seeded into its own temporary state directory (core
``MEMORY.md``/``USER.md``, structured records including superseded ones, and a
Session Recall archive with simulated timestamps). The real ``ReActAgent`` then
runs the scenario's conversation with a capturing fake provider. Injected text
is the difference between each provider request and the conversation's
canonical history plus the base system prompt.

Live (opt-in; calls the configured model and costs tokens)::

    python -m agent.evals.memory.run --live --model <catalog-key> --mode session --out live.json

The model configuration is resolved like the backend (process environment,
the project ``.env``, the persisted selection) before any state is isolated;
everything the agent reads or writes otherwise stays in temporary directories.
From a git worktree without its own ``.env``, set ``ASTRA_ENV_FILE`` to the
checkout's ``.env`` (or export the provider key) before running.
Answers are scored by keyword/abstention rules. Write-path probes (state a fact,
ask in a fresh conversation) run too unless ``--skip-write-path``.

Context Index modes are ``off`` (MemoryRouter recall), ``session`` and ``all``
(the native index replaces MemoryRouter recall). Embeddings are always off.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .fixture import FIXTURE, load_fixture
from .harness import CONTEXT_INDEX_MODES, run_offline
from .report import render_markdown

PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _modes(values: list[str] | None) -> list[str]:
    modes: list[str] = []
    for value in values or ["off,session,all"]:
        for mode in value.split(","):
            mode = mode.strip()
            if mode not in CONTEXT_INDEX_MODES:
                raise SystemExit(f"--mode must be one of {', '.join(CONTEXT_INDEX_MODES)} (got {mode!r})")
            if mode not in modes:
                modes.append(mode)
    return modes


def _git_head() -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "?"
    return result.stdout.strip() or "?"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", action="append", help="off, session or all; repeat or comma-separate (default: all three)")
    parser.add_argument("--out", type=Path, help="write the JSON report here (Markdown summary next to it)")
    parser.add_argument("--fixture", type=Path, default=FIXTURE)
    parser.add_argument("--scenario", action="append", help="limit to scenario id(s)")
    parser.add_argument("--keep-state", type=Path, help="seed into this directory and keep it for inspection")
    parser.add_argument("--live", action="store_true", help="answer with the configured model (opt-in, costs tokens)")
    parser.add_argument("--model", help="live: model catalog key (default: LLM_MODEL or the persisted selection)")
    parser.add_argument("--max-iterations", type=int, default=8, help="live: ReAct iteration limit per turn")
    parser.add_argument("--skip-write-path", action="store_true", help="live: skip the write-path probes")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    fixture = load_fixture(args.fixture).select(args.scenario)
    modes = _modes(args.mode)
    progress = None if args.quiet else (lambda line: print(line, file=sys.stderr, flush=True))
    if args.keep_state:
        state_root = args.keep_state.resolve()
        state_root.mkdir(parents=True, exist_ok=True)
    else:
        state_root = Path(tempfile.mkdtemp(prefix="astra-memory-eval-")).resolve()
    try:
        if args.live:
            from .live import run_live

            report: dict[str, Any] = run_live(
                fixture, modes, state_root, model_key=args.model, max_iterations=args.max_iterations,
                include_write_path=not args.skip_write_path, progress=progress,
            )
        else:
            report = run_offline(fixture, modes, state_root, progress=progress)
    finally:
        if not args.keep_state:
            shutil.rmtree(state_root, ignore_errors=True)
    report["meta"] = {
        "tier": "live" if args.live else "offline",
        "fixture": str(args.fixture.name),
        "fixture_sha256": fixture.sha256,
        "scenarios": len(fixture.scenarios),
        "probes": len(fixture.probes),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "git_head": _git_head(),
        "state_root": str(state_root) if args.keep_state else "(temporary, removed)",
    }
    report = {"meta": report.pop("meta"), **report}
    markdown = render_markdown(report)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        args.out.with_suffix(".md").write_text(markdown, encoding="utf-8")
    print(markdown)
    violations = report.get("isolation", {}).get("violations", [])
    if violations:
        print(f"Isolation violations: {violations}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
