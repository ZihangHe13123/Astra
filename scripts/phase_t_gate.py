"""Cross-platform automated Phase T release gate."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def resolve_tool_paths(platform: str | None = None) -> dict[str, Path | str]:
    """Return the release-gate executables for a Windows or POSIX host."""
    current_platform = sys.platform if platform is None else platform
    is_windows = current_platform == "win32"
    venv_bin = PROJECT_ROOT / ".venv" / ("Scripts" if is_windows else "bin")
    executable_suffix = ".exe" if is_windows else ""
    return {
        "python": venv_bin / f"python{executable_suffix}",
        "ruff": venv_bin / f"ruff{executable_suffix}",
        "pyright": venv_bin / f"pyright{executable_suffix}",
        "npm": "npm.cmd" if is_windows else "npm",
    }


_TOOLS = resolve_tool_paths()
PYTHON = _TOOLS["python"]
RUFF = _TOOLS["ruff"]
PYRIGHT = _TOOLS["pyright"]
NPM = _TOOLS["npm"]
VENV_BIN = Path(PYTHON).parent


def run(
    label: str,
    command: list[str],
    *,
    cwd: Path = PROJECT_ROOT,
    env: Mapping[str, str] | None = None,
) -> None:
    print(f"[Phase T] {label}", flush=True)
    completed = subprocess.run(command, cwd=cwd, env=dict(env) if env is not None else None, check=False)
    if completed.returncode:
        raise SystemExit(f"{label} failed with exit code {completed.returncode}")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider-smoke", action="store_true")
    parser.add_argument("--model-key", default="")
    parser.add_argument("--context-index-performance", action="store_true",
                        help="run the production-sized Context Index acceptance tests separately")
    parser.add_argument("--native", action="store_true", help="run serial Swift tests (macOS only)")
    parser.add_argument("--wheel-smoke", action="store_true",
                        help="build and check the wheel offline, outside the checkout (requires uv cache)")
    parser.add_argument("--keep-going", action="store_true",
                        help="finish independent checks after failures, then fail the gate if any check failed")
    args = parser.parse_args(argv)
    if args.native and sys.platform != "darwin":
        parser.error("--native requires macOS and the Swift/Xcode toolchain")
    return args


def discover_tui_tests(source_root: Path) -> list[Path]:
    """Return every TypeScript TUI test supported by Node's test runner."""
    return sorted((*source_root.rglob("*.test.ts"), *source_root.rglob("*.test.tsx")))


def _run_gate(args: argparse.Namespace, gate_temp: Path) -> None:
    failures: list[str] = []

    def check(
        label: str,
        command: list[str],
        *,
        cwd: Path = PROJECT_ROOT,
        env: Mapping[str, str] | None = None,
    ) -> None:
        try:
            run(label, command, cwd=cwd, env=env)
        except (SystemExit, OSError) as exc:
            if not args.keep_going:
                raise
            failures.append(label)
            print(f"[Phase T] {label} failed: {exc}", flush=True)

    check("Ruff", [str(RUFF), "check", "agent", "tests", "scripts"])
    check("Pyright", [str(PYRIGHT), "agent", "scripts/phase_t_provider_smoke.py",
                    "scripts/phase_t_gate.py", "scripts/wheel_smoke.py"])

    core_root = PROJECT_ROOT / "ui-core"
    check("Shared UI build", [str(NPM), "run", "build"], cwd=core_root)
    check("Shared UI tests", [str(NPM), "test"], cwd=core_root)

    tui_root = PROJECT_ROOT / "ui-tui"
    test_files = [str(path) for path in discover_tui_tests(tui_root / "src")]
    check("TUI tests", ["node", "--import", "tsx", "--test", *test_files], cwd=tui_root)
    check("TUI typecheck/build", [str(NPM), "run", "build"], cwd=tui_root)

    if args.provider_smoke:
        command = [str(PYTHON), "scripts/phase_t_provider_smoke.py"]
        if args.model_key:
            command.extend(["--model-key", args.model_key])
        check("real provider 4K smoke", command)
    else:
        print("[Phase T] provider smoke skipped; rerun with --provider-smoke before release")
    print("[Phase T] manual Ink restart/session check remains a human release step")
    check(
        "Python tests",
        [str(PYTHON), "-m", "pytest", "-q", "-m", "not context_index_performance",
         f"--basetemp={gate_temp / 'pytest'}"],
    )
    if args.wheel_smoke:
        check("isolated wheel smoke", [str(PYTHON), "scripts/wheel_smoke.py"])
    if args.context_index_performance:
        check(
            "Context Index performance",
            [str(PYTHON), "-m", "pytest", "-q", "-s", "tests/test_context_index_performance.py",
             f"--basetemp={gate_temp / 'context-index-performance'}"],
            env={**os.environ, "ASTRA_RUN_CONTEXT_INDEX_PERF": "1",
                 "ASTRA_CONTEXT_INDEX_EMBEDDING": "off",
                 "ASTRA_CONTEXT_INDEX_VECTORS_DB": str(gate_temp / "absent-vectors.sqlite3")},
        )
    if args.native:
        # Keep build products and test state out of the user's durable .astra.
        # No simultaneous Python/Swift workload contaminates the latency check.
        check("shared Appshot Swift tests", [
            "swift", "test", "--package-path", str(PROJECT_ROOT / "native/appshot-core"),
            "--scratch-path", str(gate_temp / "appshot-core-build"), "--no-parallel",
        ])
        check("macOS Swift tests", [
            "swift", "test", "--package-path", str(PROJECT_ROOT / "native/macos-computer-helper"),
            "--scratch-path", str(gate_temp / "swift-build"), "--no-parallel",
            "--disable-automatic-resolution",
        ])
    if failures:
        raise SystemExit(f"[Phase T] automated gate failed: {', '.join(failures)}")
    print("[Phase T] automated gate passed")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="astra-phase-t-") as temporary:
        _run_gate(args, Path(temporary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
