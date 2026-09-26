"""Locked source environments, their health probes and freshness records."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sys
from pathlib import Path

from .common import LauncherError, read_json, run, write_json
from .installation import Installation, runtime_environment

PYTHON_PROBE = "import openai, yaml, httpx, mcp, opentelemetry, websockets, PIL"
LOCK_FILES = ("pyproject.toml", "uv.lock", "ui-tui/package.json", "ui-tui/package-lock.json")
TUI_NODE_MIN_VERSION = (18, 0, 0)
GUI_NODE_MIN_VERSION = (22, 12, 0)


def gui_enabled(install: Installation) -> bool:
    return bool(install.metadata.get("gui"))


def source_ui_packages(install: Installation, *, gui: bool | None = None) -> list[Path]:
    packages = []
    if (install.root / "ui-core/package.json").is_file():
        packages.append(install.root / "ui-core")
    packages.append(install.ui)
    include_gui = gui if gui is not None else gui_enabled(install)
    if include_gui:
        packages.append(install.root / "ui-gui")
    return packages


def fingerprint(install: Installation) -> str:
    digest = hashlib.sha256()
    digest.update(f"{sys.platform}/{platform.machine()}".encode())
    names: list[str] = list(LOCK_FILES)
    if (install.root / "ui-core/package.json").is_file():
        names.extend(("ui-core/package.json", "ui-core/package-lock.json"))
    for name in names:
        path = install.root / name
        if not path.is_file():
            raise LauncherError(f"Missing {path}. Reinstall the complete source checkout.")
        digest.update(name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def gui_fingerprint(install: Installation) -> str:
    digest = hashlib.sha256(fingerprint(install).encode())
    for name in ("package.json", "package-lock.json"):
        path = install.root / "ui-gui" / name
        if not path.is_file():
            raise LauncherError("Desktop source is missing. Update this checkout before running astra setup --gui.")
        digest.update(path.read_bytes())
    return digest.hexdigest()


def gui_ready(install: Installation) -> bool:
    state = read_json(install.control / "gui-environment.json")
    return (gui_enabled(install) and state.get("root") == str(install.root)
            and state.get("fingerprint") == gui_fingerprint(install)
            and all((install.root / "ui-gui" / name).is_file() for name in
                    ("node_modules/electron/cli.js", "dist/main.cjs", "dist/preload.cjs", "dist/renderer/index.html")))


def gui_health(install: Installation) -> None:
    check_node(install.root, gui=True)
    gui_executable(install)


def gui_executable(install: Installation) -> str:
    # Requiring Electron 44 can download binaries. Health checks and launches
    # must remain read-only; only explicit setup/update installs components.
    package = install.root / "ui-gui/node_modules/electron"
    marker = package / "path.txt"
    path = (package / "dist" / marker.read_text(encoding="utf-8").strip()).resolve() if marker.is_file() else package
    if not path.is_relative_to((package / "dist").resolve()) or not path.is_file():
        raise LauncherError("Electron binary missing. Run astra setup --gui --repair.")
    return str(path)


def node_command() -> str:
    node = shutil.which("node")
    if not node:
        raise LauncherError("The Node.js executable was not found on PATH.")
    return node


def check_node(root: Path, *, gui: bool = False) -> str:
    """Read-only prerequisite shared by setup, maintenance and health checks."""
    minimum = GUI_NODE_MIN_VERSION if gui else TUI_NODE_MIN_VERSION
    requirement = f"{'GUI' if gui else 'TUI'} requires Node.js {'.'.join(map(str, minimum))} or newer"
    recovery = ("Install a supported Node.js LTS (including npm), open a new terminal, "
                f"then run astra setup{' --gui' if gui else ''} --repair.")
    try:
        version = run([node_command(), "-p", "process.versions.node"], cwd=root, timeout=30)
    except LauncherError as exc:
        raise LauncherError(f"{requirement}. {exc} {recovery}") from exc
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise LauncherError(f"{requirement}; could not determine a stable Node.js version. {recovery}")
    if tuple(map(int, version.split("."))) < minimum:
        raise LauncherError(f"{requirement}; found {version}. {recovery}")
    return version


def node_health(install: Installation) -> None:
    check_node(install.root)
    if install.kind == "source":
        run([node_command(), "--input-type=module", "-e", "await import('react'); await import('ink'); await import('tsx');"],
            cwd=install.ui, timeout=60)


def npm_command() -> str:
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        raise LauncherError("npm is missing. Install it with Node.js, open a new terminal, then run astra setup.")
    return npm


def python_health(install: Installation) -> None:
    if not install.python.is_file():
        raise LauncherError(f"Python environment missing: {install.python}. Run astra setup.")
    run([str(install.python), "-c", "import sys; assert sys.version_info >= (3,11); " + PYTHON_PROBE],
        cwd=install.root, timeout=60)


def is_ready(install: Installation) -> bool:
    state = read_json(install.control / "environment.json")
    return (not install.metadata.get("relocated") and state.get("root") == str(install.root)
            and state.get("fingerprint") == fingerprint(install)
            and install.python.is_file() and (install.ui / "node_modules/tsx/dist/cli.mjs").is_file())


def uv_command(install: Installation | None = None) -> str:
    local = install.control / "bootstrap" / ("Scripts/uv.exe" if os.name == "nt" else "bin/uv") if install else None
    command = str(local) if local and local.is_file() else shutil.which("uv")
    if not command:
        raise LauncherError("uv is required for locked setup/update. Install it from https://docs.astral.sh/uv/"
                            "getting-started/installation/ and run astra setup again.")
    return command


def ensure_uv(install: Installation) -> str:
    try:
        return uv_command(install)
    except LauncherError:
        pass
    folder = install.control / "bootstrap"
    python = folder / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if folder.is_symlink():
        raise LauncherError(f"Bootstrap environment must not be shared: {folder}")
    print("Preparing a private uv installation (the same version used by Astra's maintenance checks)…", flush=True)
    run([str(getattr(sys, "_base_executable", sys.executable)), "-m", "venv", str(folder)],
        cwd=install.root, capture=False, timeout=180)
    run([str(python), "-m", "pip", "install", "uv==0.11.27"], cwd=install.root, capture=False, timeout=600)
    return uv_command(install)


def enabled_extras(install: Installation, requested: list[str] | None = None) -> list[str]:
    extras = set(install.metadata.get("extras", ["mcp", "tracing"]))
    if not install.metadata and install.python.is_file():
        # Adopt extras already in use when upgrading an older checkout that has
        # no installation record. This imports metadata, not the native packages.
        output = run([str(install.python), "-c", "import importlib.metadata as m,json;"
                      "print(json.dumps(sorted({d.metadata['Name'].lower() for d in m.distributions() if d.metadata['Name']})))"],
                     cwd=install.root)
        installed = set(json.loads(output))
        for package, extra in {"pytest": "dev", "uvicorn": "server", "nbclient": "notebook",
                               "hindsight-client": "hindsight", "mlx-embeddings": "embedding", "textual": "legacy-tui"}.items():
            if package in installed:
                extras.add(extra)
    extras.update(requested or [])
    return sorted(extras)


def check_environment_ownership(install: Installation) -> None:
    if install.metadata.get("relocated") and any((install.root / name).exists() for name in (".venv", "ui-tui/node_modules")):
        raise LauncherError("This installation moved or was copied. Move .venv and ui-tui/node_modules aside, "
                            "keep .astra and .sessions, then run astra setup to recreate environments at this path.")
    generated = [install.root / ".venv", *(install.root / ui / directory
                 for ui in ("ui-tui", "ui-core", "ui-gui") for directory in ("node_modules", "dist"))]
    for path in generated:
        if path.is_symlink() or (path.exists() and path.resolve() != path.absolute()):
            raise LauncherError(f"Shared/symlinked generated directory: {path}. Update its owning checkout instead.")
        if path.exists() and not path.is_dir():
            raise LauncherError(f"Expected a generated directory at {path}.")
    other_python = install.root / ".venv" / ("bin/python" if os.name == "nt" else "Scripts/python.exe")
    if other_python.exists() and not install.python.exists():
        raise LauncherError("This .venv belongs to another platform. Move only .venv aside and run astra setup;"
                            " keep .astra and .sessions.")


def synchronize(install: Installation, extras: list[str], *, python: bool = True, node: bool = True, build: bool = True) -> None:
    uv = uv_command(install)
    check_node(install.root, gui=gui_enabled(install))
    npm = npm_command()
    env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(install.root / ".venv"))
    env.pop("VIRTUAL_ENV", None)
    # Explicit environment ownership; do not let inherited uv flags redirect installation.
    env.pop("UV_PROJECT", None)
    env.pop("UV_WORKING_DIRECTORY", None)
    command = [uv, "sync", "--locked", "--inexact", "--python", str(getattr(sys, "_base_executable", sys.executable))]
    for extra in extras:
        command.extend(["--extra", extra])
    if python:
        print("Synchronizing Python dependencies from uv.lock…", flush=True)
        run(command, cwd=install.root, env=env, capture=False, timeout=1200)
        run([uv, "pip", "check", "--python", str(install.python)], cwd=install.root, env=env)
    if node:
        print("Synchronizing interface dependencies from package-lock.json…", flush=True)
        for package in source_ui_packages(install):
            run([npm, "ci", "--no-audit", "--no-fund"], cwd=package, capture=False, timeout=900)
        if gui_enabled(install):
            run([node_command(), "node_modules/electron/install.js"], cwd=install.root / "ui-gui",
                capture=False, timeout=900)
    if build:
        for package in source_ui_packages(install):
            run([npm, "run", "build"], cwd=package, capture=False, timeout=600)


def validate(install: Installation) -> None:
    python_health(install)
    node_health(install)
    if gui_enabled(install):
        gui_health(install)
    if not (install.ui / "node_modules/tsx/dist/cli.mjs").is_file():
        raise LauncherError("The Ink interface is incomplete. Run astra setup.")
    # Syntax check all application modules without starting model clients or services.
    run([str(install.python), "-m", "compileall", "-q", "agent", "astra.py"], cwd=install.root)
    env = runtime_environment(install, install.root)
    probe = """from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from agent.launcher.installation import discover
from agent.launcher.locking import protect_backend
with TemporaryDirectory(prefix='astra-startup-probe-') as directory:
    root = Path(directory)
    install = replace(discover(), control=root/'control', data=root/'data', sessions=root/'sessions')
    with protect_backend(install):
        from agent.cli import backend
        from agent.launcher.cli import main
"""
    run([str(install.python), "-c", probe],
        cwd=install.root, env=env, timeout=90)


def record_environment(install: Installation, extras: list[str]) -> None:
    metadata = install.metadata
    metadata.pop("relocated", None)
    metadata.update(schema=1, kind="source", owner=install.owner, root=str(install.root), extras=extras)
    write_json(install.control / "installation.json", metadata)
    write_json(install.control / "environment.json", {
        "schema": 1, "root": str(install.root), "fingerprint": fingerprint(install), "extras": extras,
        "python": run([str(install.python), "-c", "import sys;print(sys.version.split()[0])"], cwd=install.root),
        "node": check_node(install.root, gui=gui_enabled(install)),
    })
    if gui_enabled(install):
        write_json(install.control / "gui-environment.json", {
            "schema": 1, "root": str(install.root), "fingerprint": gui_fingerprint(install),
        })


def diagnostics(install: Installation) -> dict:
    report = install.describe()
    problems = []
    for label, probe in (("python", lambda: python_health(install)),
                         ("node", lambda: node_health(install))):
        try:
            probe()
        except LauncherError as exc:
            problems.append({"component": label, "message": str(exc)})
    entry = install.ui / ("src/index.tsx" if install.kind == "source" else "dist/tui.mjs")
    if not entry.is_file():
        problems.append({"component": "interface", "message": f"Missing {entry}. "
                         "The Python-only wheel does not include Ink; use a complete source installation."})
    if install.kind == "source":
        try:
            if not is_ready(install):
                problems.append({"component": "environment", "message": "Dependencies are unverified or changed. Run astra setup."})
        except LauncherError as exc:
            problems.append({"component": "environment", "message": str(exc)})
        if gui_enabled(install):
            try:
                gui_health(install)
                if not gui_ready(install):
                    raise LauncherError("Desktop dependencies are unverified or changed. Run astra setup --gui.")
            except LauncherError as exc:
                problems.append({"component": "gui", "message": str(exc)})
        report["gui"] = {"enabled": gui_enabled(install),
                         "healthy": gui_enabled(install) and not any(p["component"] == "gui" for p in problems)}
    if report["pending_update"]:
        problems.append({"component": "update", "message": "Interrupted update: run astra update --recover."})
    if report["pending_services"]:
        problems.append({"component": "services", "message": "Companion services need recovery: run astra update --recover."})
    command = shutil.which("astra")
    report.update(command_on_path=command, problems=problems, healthy=not problems)
    return report
