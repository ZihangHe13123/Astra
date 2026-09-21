"""Journaled source updates with same-path generated-environment recovery."""

from __future__ import annotations

import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .common import LauncherError, git, read_json, write_json
from .installation import Installation
from .local_changes import LocalChanges

GENERATED = (".venv", "ui-tui/node_modules", "ui-tui/dist", "ui-core/node_modules", "ui-core/dist",
             "ui-gui/node_modules", "ui-gui/dist")
RECORDS = ("installation.json", "environment.json", "gui-environment.json")


def receipt(install: Installation, **fields) -> dict:
    result = {"schema": 1, "root": str(install.root), "time": datetime.now(timezone.utc).isoformat(), **fields}
    folder = install.control / "receipts"
    write_json(folder / f"{uuid.uuid4().hex}.json", result)
    write_json(folder / "latest.json", result)
    return result


def _remove_directory(path: Path) -> None:
    if path.is_symlink():
        raise LauncherError(f"Refusing to remove a replaced/symlinked directory: {path}")
    if path.exists():
        shutil.rmtree(path)


class Transaction:
    def __init__(self, install: Installation, before: str, target: str, *, generated: tuple[str, ...] = GENERATED):
        self.install = install
        self.pending = install.control / "pending.json"
        self.directory = install.control / "transactions" / uuid.uuid4().hex
        self.generated = generated
        self.local: LocalChanges | None = None
        self.state = {"schema": 1, "root": str(install.root), "directory": str(self.directory),
                      "before": before, "target": target, "phase": "prepared", "generated": {}, "records": {}}

    def attach_local(self, local: LocalChanges, policy: str) -> None:
        self.local = local
        directory = local.backup(policy)
        self.state["local_changes"] = {"directory": str(directory), "policy": policy, "phase": "saved"}

    def clear_local(self) -> None:
        if self.local:
            self.state["local_changes"]["phase"] = "clearing"
            self.save()
            self.local.clear()
            self.state["local_changes"]["phase"] = "cleared"
            self.save()

    def apply_local(self) -> None:
        if self.local:
            self.state["local_changes"]["phase"] = "applying"
            self.save()
            if self.state["local_changes"]["policy"] == "keep":
                self.local.apply()
            else:
                self.local.require_owned()
            self.state["local_changes"]["phase"] = "applied"
            self.save()

    def local_receipt(self) -> dict:
        if not self.local:
            return {}
        return {"local_files": self.local.paths, "local_policy": self.state["local_changes"]["policy"],
                "local_backup": str(self.local.directory)}

    def save(self, phase: str | None = None) -> None:
        if phase:
            self.state["phase"] = phase
        write_json(self.pending, self.state)

    def prepare(self) -> None:
        if self.pending.exists():
            raise LauncherError("An earlier transaction needs astra update --recover.")
        self.directory.mkdir(parents=True)
        total = sum(path.stat().st_size for name in self.generated
                    for path in (self.install.root / name).rglob("*") if path.is_file() and not path.is_symlink())
        if shutil.disk_usage(self.directory).free < total + 64 * 1024 * 1024:
            raise LauncherError("Insufficient space to preserve the previous environment for recovery.")
        for name in RECORDS:
            path = self.install.control / name
            self.state["records"][name] = path.read_text(encoding="utf-8") if path.exists() else None
        self.save()
        for name in self.generated:
            original = self.install.root / name
            backup = self.directory / name
            existed = original.exists()
            self.state["generated"][name] = {"existed": existed}
            self.save()
            if existed:
                backup.parent.mkdir(parents=True, exist_ok=True)
                original.rename(backup)
                # Recreate at the canonical path. Existing venv scripts retain absolute paths;
                # the parked copy is only a same-machine, same-path recovery snapshot.
                shutil.copytree(backup, original, symlinks=True)
        self.save("environments_saved")

    def commit(self) -> dict:
        result = receipt(self.install, outcome="applied", before=self.state["before"], target=self.state["target"],
                         activation="next launch", user_data="preserved", **self.local_receipt())
        self.save("committed")
        self.pending.unlink()
        shutil.rmtree(self.directory, ignore_errors=True)
        return result

    @classmethod
    def load(cls, install: Installation):
        state = read_json(install.control / "pending.json")
        if not state:
            raise LauncherError("No interrupted update to recover.")
        directory = Path(state.get("directory", "")).resolve()
        if (state.get("schema") != 1 or state.get("root") != str(install.root)
                or directory.parent != (install.control / "transactions").resolve()
                or not directory.is_dir()
                or set(state.get("generated", {})) - set(GENERATED)
                or set(state.get("records", {})) - set(RECORDS)):
            raise LauncherError("Invalid recovery journal. Preserve it and inspect with astra doctor.")
        transaction = cls(install, state["before"], state["target"])
        transaction.directory = directory
        transaction.state = state
        if state.get("local_changes"):
            local = state["local_changes"]
            if local.get("policy") not in {"keep", "overwrite"} or local.get("phase") not in {
                    "saved", "clearing", "cleared", "applying", "applied"}:
                raise LauncherError("Invalid local-change recovery state.")
            transaction.local = LocalChanges.load(install, local["directory"])
            if (transaction.local.before, transaction.local.target) != (state["before"], state["target"]):
                raise LauncherError("Local backup does not belong to this update transaction.")
        return transaction

    def recover(self) -> dict:
        if self.state["phase"] == "committed":
            result = receipt(self.install, outcome="applied", before=self.state["before"],
                             target=self.state["target"], activation="next launch", **self.local_receipt())
        else:
            before, target = self.state["before"], self.state["target"]
            restore_local = self.local is not None and self.state["local_changes"]["phase"] != "saved"
            if before and target != before:
                head = git(self.install.root, "rev-parse", "HEAD")
                if head not in {before, target}:
                    raise LauncherError("HEAD changed outside the updater. Recovery paused to preserve your work.")
                if restore_local and self.local:
                    self.local.prepare_recovery()
                if head == target:
                    # --keep refuses to overwrite local edits created after the update began.
                    git(self.install.root, "reset", "--keep", before)
            for name, details in self.state["generated"].items():
                original, backup = self.install.root / name, self.directory / name
                if backup.exists():
                    _remove_directory(original)
                    backup.rename(original)
                elif not details["existed"]:
                    _remove_directory(original)
            for name, content in self.state["records"].items():
                path = self.install.control / name
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    # Metadata snapshots were captured before any mutation.
                    import json
                    write_json(path, json.loads(content))
            if restore_local and self.local:
                self.local.restore_original()
            result = receipt(self.install, outcome="recovered", before=before, target=target,
                             user_data="preserved", **self.local_receipt())
        self.pending.unlink(missing_ok=True)
        shutil.rmtree(self.directory, ignore_errors=True)
        return result
