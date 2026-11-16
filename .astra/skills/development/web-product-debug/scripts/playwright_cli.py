#!/usr/bin/env python3
"""Run a pinned Playwright CLI in one project's named artifact directory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys


PACKAGE = "@playwright/cli@0.1.24"
_SESSION = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}\Z")
_RESERVED = {"con", "prn", "aux", "nul"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
}


class LauncherError(Exception):
    """An actionable setup error, before a browser command is dispatched."""


def find_runner(platform: str | None = None) -> list[str]:
    """Avoid cmd.exe on Windows; preserve each CLI argument as an argument."""
    platform = platform or sys.platform
    node = shutil.which("node")
    if not node:
        raise LauncherError("Node.js is missing from PATH. Install Node.js LTS with npm, then retry.")
    npx = shutil.which("npx.cmd" if platform == "win32" else "npx")
    if not npx:
        raise LauncherError("npx is missing from PATH. Repair the Node.js/npm installation, then retry.")
    if platform != "win32":
        return [npx]

    # Standard Node, nvm-windows, and npm prefix installs place npm beside one
    # of these executables. Resolve shims/symlinks before looking beside them.
    for executable in (npx, node):
        script = Path(executable).resolve().parent / "node_modules" / "npm" / "bin" / "npx-cli.js"
        if script.is_file():
            return [node, str(script)]
    raise LauncherError(
        "Found node and npx.cmd, but not npm/bin/npx-cli.js beside them. "
        "Put the actual Node.js/npm installation on PATH; batch-file fallback is not used."
    )


def artifact_directory(workspace: str, session: str) -> Path:
    if not _SESSION.fullmatch(session) or session in _RESERVED:
        raise LauncherError(
            "Session must be 1-64 lowercase ASCII letters, digits, '-' or '_', start with a letter/digit, "
            "and not be a reserved Windows filename."
        )
    root = Path(workspace).expanduser()
    if not root.is_absolute() or not root.is_dir():
        raise LauncherError("--workspace must name an existing absolute project directory.")
    root = root.resolve()
    directory = (root / "output" / "playwright" / session).resolve()
    if not directory.is_relative_to(root):
        raise LauncherError("The artifact directory resolves outside the workspace. Check output directory links.")
    return directory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Playwright product debugging with a named session and project-local artifacts.",
        epilog="Example: --workspace /absolute/project --session ui-review -- open http://localhost:3000",
    )
    parser.add_argument("--workspace", required=True, help="Existing absolute project directory")
    parser.add_argument("--session", required=True, help="Stable lowercase name unique to this task")
    parser.add_argument("--offline", action="store_true", help="Use npm's cache without network access")
    parser.add_argument("--check", action="store_true", help="Report prerequisites and paths without running npx")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="Playwright CLI command, after --")
    options = parser.parse_args(argv)
    command = options.command
    if command[:1] == ["--"]:
        command = command[1:]
    elif command:
        parser.error("Place the Playwright CLI command after --.")
    if options.check and command:
        parser.error("--check does not accept a browser command.")
    if not options.check and not command:
        parser.error("A Playwright CLI command after -- is required; no browser was started.")

    try:
        directory = artifact_directory(options.workspace, options.session)
        runner = find_runner()
        if options.check:
            print(json.dumps({
                "package": PACKAGE,
                "runner": runner,
                "session": options.session,
                "cwd": str(directory),
                "offline": options.offline,
                "note": "Dependency paths found; CLI cache and browser availability have not been probed.",
            }, ensure_ascii=False, indent=2))
            return 0
        directory.mkdir(parents=True, exist_ok=True)
        args = [*runner, "--yes", *(["--offline"] if options.offline else []),
                "--package", PACKAGE, "playwright-cli", f"-s={options.session}", *command]
        # Inherit stdout/stderr instead of collecting large traces or altering
        # CLI errors. No shell interpolation, implicit retry, or global cleanup.
        return subprocess.run(args, cwd=directory, shell=False, check=False).returncode
    except LauncherError as exc:
        print(f"Playwright setup: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Playwright launch failed: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted. Observe the named browser session before retrying a write; its outcome may be unknown.",
              file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
