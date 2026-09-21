"""Optional desktop launch; the detached supervisor holds the installation lease."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import dependencies
from .common import LauncherError
from .installation import Installation, discover, runtime_environment
from .locking import RuntimeLease


def launch_gui(install: Installation) -> int:
    if install.kind != "source":
        raise LauncherError("This distribution does not include the desktop interface. Use a complete source checkout.")
    if not dependencies.is_ready(install) or not dependencies.gui_ready(install):
        raise LauncherError("Run astra setup --gui once to prepare this checkout's desktop dependencies.")
    dependencies.python_health(install)
    dependencies.gui_health(install)
    directory = install.data / "gui" / "launches"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    request = uuid.uuid4().hex
    ready = directory / f"{request}.json"
    log = directory / f"{request}.log"
    env = runtime_environment(install, Path.cwd())
    creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    with log.open("xb") as output:
        if os.name != "nt":
            log.chmod(0o600)
        child = subprocess.Popen([str(install.python), "-m", "agent.launcher.gui", "--root", str(install.root),
                                  "--ready", str(ready)], cwd=install.root, env=env,
                                 stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                 creationflags=creationflags, start_new_session=os.name != "nt")
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if ready.is_file():
            ready.unlink(missing_ok=True)
            print("Astra desktop opened. Closing this terminal will not stop the window.")
            return 0
        if child.poll() is not None:
            break
        time.sleep(0.1)
    if child.poll() is None:
        child.terminate()
    raise LauncherError(f"Desktop startup did not complete. See {log}")


def supervise(root: Path, ready: Path) -> int:
    install = discover(root)
    with RuntimeLease(install, "desktop"):
        executable = dependencies.gui_executable(install)
        env = runtime_environment(install, Path(os.environ.get("ASTRA_WORKSPACE", str(root))))
        env.update(ASTRA_GUI_READY_FILE=str(ready), ASTRA_LAUNCHER_PID=str(os.getpid()))
        env.pop("ELECTRON_RUN_AS_NODE", None)
        process = subprocess.Popen([executable, str(root / "ui-gui")], cwd=root, env=env,
                                   stdin=subprocess.DEVNULL)

        def stop(_signum, _frame):
            if process.poll() is None:
                process.terminate()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        return process.wait()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--ready", type=Path, required=True)
    options = parser.parse_args()
    try:
        return supervise(options.root, options.ready)
    except (LauncherError, OSError) as exc:
        print(f"Astra desktop: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
