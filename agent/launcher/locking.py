"""Install-scoped update exclusion and process-lifetime runtime leases."""

from __future__ import annotations

import atexit
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from agent.runtime.instance_lock import InstanceAlreadyRunning, InstanceLock

from .common import LauncherError, read_json, write_json
from .installation import Installation, discover, runtime_environment


def active_instances(install: Installation) -> list[dict]:
    active = []
    folder = install.control / "instances"
    if not folder.exists():
        return active
    for path in sorted(folder.glob("*.lock")):
        lock = InstanceLock(path)
        try:
            lock.acquire()
        except InstanceAlreadyRunning:
            try:
                metadata = read_json(path.with_suffix(".json"))
            except LauncherError:
                metadata = {}
            active.append(metadata or {"role": "unknown Astra process", "pid": "unknown"})
        else:
            lock.release()
            path.with_suffix(".json").unlink(missing_ok=True)
            # Runtime lease names are unique and never reused. The shared
            # update.lock inode, in contrast, must never be unlinked.
            path.unlink(missing_ok=True)
    return active


@contextmanager
def exclusive(install: Installation, *, allow_pending: bool = False):
    try:
        with InstanceLock(install.control / "update.lock"):
            if not allow_pending and (install.control / "desktop-pending.json").exists():
                raise LauncherError("A desktop update was interrupted. Run the desktop updater recover action first.")
            if not allow_pending and any((install.control / name).exists() for name in ("pending.json", "services.json")):
                raise LauncherError("An update was interrupted. Run astra update --recover before starting Astra.")
            yield
    except InstanceAlreadyRunning as exc:
        raise LauncherError("Another Astra setup or update is running. Wait for it to finish.") from exc


class RuntimeLease:
    def __init__(self, install: Installation, role: str):
        self.install = install
        self.path = install.control / "instances" / f"{uuid.uuid4().hex}.lock"
        self.lock = InstanceLock(self.path)
        with exclusive(install):
            self.lock.acquire()
            try:
                details = install.describe()
                write_json(self.path.with_suffix(".json"), {
                    "pid": os.getpid(), "role": role, "root": str(install.root),
                    "commit": details["commit"], "version": install.version,
                    "dirty": details["dirty"],
                })
            except BaseException:
                self.lock.release()
                raise

    def close(self) -> None:
        self.path.with_suffix(".json").unlink(missing_ok=True)
        self.lock.release()
        self.path.unlink(missing_ok=True)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def protect_backend(install: Installation | None = None) -> RuntimeLease:
    """Called before optional imports when a backend is launched directly."""
    install = install or discover()
    lease = RuntimeLease(install, "backend")
    atexit.register(lease.close)
    os.environ.update(runtime_environment(install, Path.cwd()))
    return lease


def require_idle(install: Installation) -> None:
    active = active_instances(install)
    if active:
        raise LauncherError("Close these Astra instances before updating: " + json.dumps(active, ensure_ascii=False))
