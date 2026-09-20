"""Stable, dependency-free command routing before model/UI initialization."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

from . import dependencies
from .common import LauncherError
from .installation import Installation, discover, runtime_environment
from .locking import RuntimeLease, active_instances
from .setup import install_command, setup_source
from .update import update_source


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="astra", description="Start Astra or manage its installation without a model connection.")
    result.add_argument("--version", action="store_true", help="show the installed version")
    result.add_argument("--cli", action="store_true", help="use the legacy text CLI explicitly")
    result.add_argument("--tui", action="store_true", help="use the legacy Textual UI explicitly")
    result.add_argument("--ink", action="store_true", help=argparse.SUPPRESS)
    result.add_argument("--setup-only", action="store_true", help="compatibility alias for setup")
    result.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    result.add_argument("--updater-child", action="store_true", help=argparse.SUPPRESS)
    commands = result.add_subparsers(dest="command")
    for name, description in (("version", "Show versions, paths and installation ownership"),
                              ("doctor", "Check the local installation without network access")):
        item = commands.add_parser(name, help=description)
        item.add_argument("--json", action="store_true")
    setup = commands.add_parser("setup", help="Prepare the locked source environment")
    setup.add_argument("--extra", action="append", default=[], help="enable an optional Python dependency group")
    setup.add_argument("--repair", action="store_true", help="resynchronize even if the environment is recorded as current")
    setup.add_argument("--install-command", action="store_true", help="install a dedicated per-user astra command")
    setup.add_argument("--command-only", action="store_true", help="install the command without changing dependencies")
    setup.add_argument("--bin-dir", type=Path, help="override the command directory")
    setup.add_argument("--no-path", action="store_true", help="do not change the user's PATH configuration")
    setup.add_argument("--json", action="store_true")
    update = commands.add_parser("update", help="Update a source checkout and its locked dependencies")
    action = update.add_mutually_exclusive_group()
    action.add_argument("--check", action="store_true", help="fetch and compare without replacing the runtime")
    action.add_argument("--recover", action="store_true", help="recover an interrupted setup/update")
    action.add_argument("--repair", action="store_true", help="repair dependencies at the current commit without fetching code")
    update.add_argument("--json", action="store_true")
    local = update.add_mutually_exclusive_group()
    local.add_argument("--keep-local", action="store_true", help="keep locally changed files exactly; update other files")
    local.add_argument("--overwrite-local", action="store_true", help="back up local files, then use the incoming versions")
    activity = commands.add_parser("activity", help="Manage activity recording (legacy command compatibility)")
    activity.add_argument("args", nargs=argparse.REMAINDER)
    auth = commands.add_parser("auth", help="Manage Astra's ChatGPT / Codex subscription login")
    auth.add_argument("action", choices=("login", "status", "logout"), nargs="?", default="status")
    return result


def isolated_maintenance(install: Installation, argv: list[str]) -> int:
    """Copy the whole updater before updating any of its own source files."""
    if os.name == "nt" and Path(sys.prefix).resolve() == (install.root / ".venv").resolve():
        raise LauncherError("Windows cannot replace the virtual environment running this command. "
                            f"Use the source launcher: {install.root / 'astra.bat'} {' '.join(argv)}")
    base = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    if base.is_relative_to(install.root / ".venv"):
        raise LauncherError("The updater needs a base Python interpreter outside .venv.")
    package = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="astra-updater-") as temporary:
        archive = Path(temporary) / "updater.pyz"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("__main__.py", "from agent.launcher.cli import main\nraise SystemExit(main())\n")
            bundle.writestr("agent/__init__.py", "")
            bundle.writestr("agent/runtime/__init__.py", "")
            bundle.write(package.parent / "runtime/instance_lock.py", "agent/runtime/instance_lock.py")
            for path in sorted(package.glob("*.py")):
                bundle.write(path, f"agent/launcher/{path.name}")
        child_env = dict(os.environ)
        for key in ("PYTHONPATH", "PYTHONHOME"):
            child_env.pop(key, None)
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, lambda signum, frame: None)
        try:
            result = subprocess.run([str(base), "-I", str(archive), "--root", str(install.root),
                                     "--updater-child", *argv], env=child_env, check=False)
            return result.returncode
        finally:
            signal.signal(signal.SIGINT, previous)


def launch(install: Installation, *, cli: bool = False, textual: bool = False, activity: list[str] | None = None) -> int:
    if install.kind == "source":
        if not dependencies.is_ready(install):
            raise LauncherError("Run astra setup once to prepare/verify this checkout's locked dependencies.")
        dependencies.python_health(install)
    workspace = Path.cwd()
    env = runtime_environment(install, workspace)
    env["ASTRA_LAUNCHER_PID"] = str(os.getpid())
    if cli or textual or activity is not None:
        command = [str(install.python), "-m", "agent.cli.main"]
        command.extend(["activity", *(activity or [])] if activity is not None else ["--tui"] if textual else [])
    else:
        dependencies.check_node(install.root)
        entry = install.ui / ("src/index.tsx" if install.kind == "source" else "dist/tui.mjs")
        if not entry.is_file():
            raise LauncherError("This distribution does not include the Ink interface. Use a complete source installation, "
                                "or astra --cli for the legacy CLI. Run astra doctor for details.")
        command = [dependencies.node_command()]
        if install.kind == "source":
            command.append(str(install.ui / "node_modules/tsx/dist/cli.mjs"))
        command.append(str(entry))
    with RuntimeLease(install, "legacy CLI" if cli else "activity" if activity is not None else "interface"):
        previous = signal.getsignal(signal.SIGINT)
        # The foreground interface owns Ctrl+C; the supervisor must not exit
        # and drop its lease while the interface is still handling cancellation.
        signal.signal(signal.SIGINT, lambda signum, frame: None)
        try:
            result = subprocess.run(command, cwd=install.root, env=env, check=False)
            return result.returncode if result.returncode >= 0 else 128 - result.returncode
        finally:
            signal.signal(signal.SIGINT, previous)


def _display(value: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False, indent=2))
        return
    for name, item in value.items():
        if name == "problems":
            for problem in item:
                print(f"  [!] {problem['component']}: {problem['message']}")
        elif item not in (None, [], {}):
            print(f"{name}: {item}")


def choose_local_files(preview: dict) -> str:
    if not sys.stdin.isatty():
        raise LauncherError("Local files need a choice. Rerun in a terminal or choose --keep-local / --overwrite-local.")
    details = preview["local_details"]
    print(f"Update {preview['before'][:8]} -> {preview['target'][:8]}", flush=True)
    print("Locally changed files (these files stay whole when kept):", flush=True)
    for name in details["files"]:
        overlap = " [also changed upstream]" if name in details["conflicts"] else ""
        print("  " + json.dumps(name, ensure_ascii=False) + overlap, flush=True)
    print("1. Keep local files; update all other files (default)\n"
          "2. Back up local files and use incoming versions\n"
          "3. Cancel", flush=True)
    while True:
        try:
            choice = input("Choose [1/2/3]: ").strip().lower()
        except EOFError:
            return "cancel"
        if choice in {"", "1", "keep"}:
            return "keep"
        if choice in {"2", "overwrite"}:
            return "overwrite"
        if choice in {"3", "cancel", "q"}:
            return "cancel"
        print("Enter 1, 2 or 3.", flush=True)


def main(argv: list[str] | None = None, *, root: Path | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    options = parser().parse_args(arguments)
    if options.command == "update" and (options.keep_local or options.overwrite_local) and (
            options.check or options.recover or options.repair):
        parser().error("Local-file choices apply to an update, not --check, --repair or --recover.")
    try:
        install = discover(root or options.root)
        command = "version" if options.version else "setup" if options.setup_only else options.command
        as_json = getattr(options, "json", False)
        if command == "auth":
            return subprocess.run([str(install.python), "-m", "agent.cli.codex_auth_cli", options.action],
                cwd=install.root, env=runtime_environment(install, Path.cwd()), check=False).returncode
        if command in {"setup", "update"}:
            if options.setup_only:
                # Normalize the old alias before the snapshot handoff/parser.
                arguments = ["setup"]
                options = parser().parse_args(arguments)
            if install.kind == "source" and not options.updater_child and not (command == "update" and options.check):
                return isolated_maintenance(install, arguments)
            with contextlib.redirect_stdout(sys.stderr):
                if command == "setup":
                    value = {"outcome": "command installed"} if options.command_only else setup_source(install, options.extra, repair=options.repair)
                    if options.install_command or options.command_only:
                        value["command"] = str(install_command(install, bin_dir=options.bin_dir, modify_path=not options.no_path))
                else:
                    policy = "keep" if options.keep_local else "overwrite" if options.overwrite_local else "ask"
                    value = update_source(install, check=options.check, recover=options.recover, repair=options.repair,
                                          local_policy=policy, choose_local=None if as_json else choose_local_files)
            _display(value, as_json)
            return 1 if value.get("services_status") == "restart_failed" else 0
        if command == "version":
            value = install.describe()
            value["running"] = active_instances(install)
            _display(value, as_json)
            return 0
        if command == "doctor":
            value = dependencies.diagnostics(install)
            value["running"] = active_instances(install)
            _display(value, as_json)
            return 0 if value["healthy"] else 1
        return launch(install, cli=options.cli, textual=options.tui,
                      activity=options.args if command == "activity" else None)
    except (LauncherError, OSError) as exc:
        print(f"Astra: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Astra: cancelled.", file=sys.stderr)
        return 130


def backend() -> int:
    # Installed console entrypoints must lease before loading native libraries.
    from .locking import protect_backend
    protect_backend()
    from agent.cli.backend import run
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
