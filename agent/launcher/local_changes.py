"""Explicit local-file preservation with private backups and recoverable index intent."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
import uuid
from pathlib import Path, PurePosixPath

from .common import LauncherError, git, git_bytes, read_json, write_json
from .installation import Installation


def _paths(value: bytes) -> list[str]:
    try:
        return [name.decode("utf-8") for name in value.split(b"\0") if name]
    except UnicodeDecodeError as exc:
        raise LauncherError("A local filename is not UTF-8. Resolve it manually before updating.") from exc


def _path(root: Path, name: str) -> Path:
    if root.is_symlink():
        raise LauncherError("The local-file root was replaced by a symlink. Recovery stopped.")
    parts = PurePosixPath(name).parts
    if not parts or name.startswith("/") or any(part.casefold() in {".", "..", ".git"} for part in parts):
        raise LauncherError("Invalid local-change path; preserve the update backup for inspection.")
    if os.name == "nt" and any("\\" in part or ":" in part for part in parts):
        raise LauncherError("Unsupported Windows local-change path.")
    path = root.joinpath(*parts)
    for parent in path.parents:
        if parent == root:
            break
        if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
            raise LauncherError(f"A local path has a replaced parent: {name!r}. Resolve it manually.")
    return path


def _read(root: Path, name: str) -> tuple[dict, bytes]:
    path = _path(root, name)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {"kind": "absent"}, b""
    if stat.S_ISLNK(info.st_mode):
        content = os.fsencode(os.readlink(path))
        kind = "symlink"
    elif stat.S_ISREG(info.st_mode):
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise LauncherError("A local file changed while preparing its backup. Retry the update.")
            content = stream.read()
        kind = "file"
    else:
        raise LauncherError(f"Local directory or special file at {name!r}; resolve this path manually.")
    after = path.lstat()
    if (info.st_ino, info.st_size, info.st_mtime_ns, info.st_mode) != (
            after.st_ino, after.st_size, after.st_mtime_ns, after.st_mode):
        raise LauncherError("A local file changed while preparing its backup. Retry the update.")
    return {"kind": kind, "sha256": hashlib.sha256(content).hexdigest(),
            "mode": stat.S_IMODE(info.st_mode)}, content


def _tree(root: Path, commit: str) -> dict[str, list[str]]:
    entries = {}
    for row in git_bytes(root, "ls-tree", "-r", "-z", commit).split(b"\0"):
        if row:
            metadata, name = row.split(b"\t", 1)
            mode, _, oid = metadata.decode("ascii").split()
            entries[_paths(name + b"\0")[0]] = [mode, oid]
    return entries


def _index(root: Path) -> dict[str, list[str]]:
    entries = {}
    for row in git_bytes(root, "ls-files", "--stage", "-z").split(b"\0"):
        if row:
            metadata, name = row.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split()
            if stage != "0":
                raise LauncherError("Resolve the existing Git merge conflicts before updating.")
            entries[_paths(name + b"\0")[0]] = [mode, oid]
    return entries


def _overlaps(left: str, right: str) -> bool:
    return left == right or left.startswith(right + "/") or right.startswith(left + "/")


class LocalChanges:
    def __init__(self, install: Installation, before: str, target: str):
        self.install, self.before, self.target = install, before, target
        self.base = _tree(install.root, before)
        self.incoming = _tree(install.root, target)
        self.index = _index(install.root)
        self.remote_paths = sorted(name for name in self.base.keys() | self.incoming.keys()
                                   if self.base.get(name) != self.incoming.get(name))
        for name in self.remote_paths:
            # Reserve aliases on every platform: a source tree prepared on Linux
            # must not overwrite private state on Windows/default macOS volumes.
            normalized = name.casefold()
            generated = tuple(f"{ui}/{part}" for ui in ("ui-tui", "ui-core", "ui-gui") for part in ("node_modules", "dist"))
            private = (normalized == ".env" or any(_overlaps(normalized, part) for part in (".sessions", ".venv", *generated))
                       or (_overlaps(normalized, ".astra") and not normalized.startswith(".astra/skills/")))
            if private:
                raise LauncherError(f"Incoming source changes target private/generated state at {name!r}. "
                                    "Resolve the upstream contents before updating; user data was preserved.")
        changed = set(_paths(git_bytes(install.root, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--")))
        changed.update(name for name in self.base.keys() | self.index.keys()
                       if self.base.get(name) != self.index.get(name))
        untracked = _paths(git_bytes(install.root, "ls-files", "--others", "--exclude-standard", "-z"))
        changed.update(name for name in untracked if any(_overlaps(name, remote) for remote in self.remote_paths))
        # Git may overwrite ignored files during checkout. Include incoming
        # collisions explicitly even when the user's ignore rules hide them.
        changed.update(name for name in self.remote_paths if name not in self.base and name not in self.index
                       and os.path.lexists(_path(install.root, name)))
        self.paths = sorted(changed)
        self.conflicts = [name for name in self.paths if any(_overlaps(name, remote) for remote in self.remote_paths)]
        self.staged = sorted(name for name in self.base.keys() | self.index.keys()
                             if self.base.get(name) != self.index.get(name))
        visible_staged = set(_paths(git_bytes(install.root, "diff", "--cached", "--ita-invisible-in-index",
                                             "--no-renames", "--name-only", "-z", "--")))
        if set(self.staged) - visible_staged:
            raise LauncherError("Git intent-to-add entries need resolution before updating. "
                                "Stage or unstage those files explicitly; no files were replaced.")
        for row in git_bytes(install.root, "ls-files", "-v", "-z").split(b"\0"):
            if row and (row[:1] == b"S" or row[:1].islower()):
                name = _paths(row[2:] + b"\0")[0]
                if name in self.paths or name in self.remote_paths:
                    raise LauncherError(f"Hidden index flags on {name!r}; resolve skip-worktree/assume-unchanged "
                                        "before updating this file.")
        self.files = {name: _read(install.root, name)[0] for name in self.paths}
        self.new_paths = {name: _read(install.root, name)[0] for name in self.remote_paths
                          if name not in self.base and name not in self.index}
        for name in self.paths:
            if any(entry.get(name, [""])[0] == "160000" for entry in (self.base, self.index, self.incoming)):
                raise LauncherError("Local submodule changes require manual resolution before updating.")
            if any(name != other and _overlaps(name, other) for other in self.paths + self.remote_paths):
                raise LauncherError(f"File/directory conflict at {name!r}; preserve it and resolve the paths manually.")
        self.conflicts = [name for name in self.conflicts if (name in self.staged and self.index.get(name) != self.incoming.get(name))
                          or not self._matches_commit(name, self.files[name], target, self.incoming)]
        self.directory: Path | None = None

    def summary(self) -> dict:
        return {"files": self.paths, "conflicts": self.conflicts, "staged_files": self.staged}

    def unchanged(self) -> None:
        if git(self.install.root, "rev-parse", "HEAD") != self.before or _index(self.install.root) != self.index:
            raise LauncherError("The checkout or index changed while preparing the update. Retry to review the new differences.")
        if any(_read(self.install.root, name)[0] != value for name, value in self.files.items()):
            raise LauncherError("A local file changed after the update preview. Retry to review the new differences.")
        if any(_read(self.install.root, name)[0] != value for name, value in self.new_paths.items()):
            raise LauncherError("An incoming path changed after the update preview. Retry to review the new differences.")
        current = set(_paths(git_bytes(self.install.root, "diff", "--name-only", "--no-renames", "-z", "HEAD", "--")))
        if current - set(self.paths):
            raise LauncherError("Additional local files changed after the update preview. Retry the update.")

    def backup(self, policy: str) -> Path:
        self.unchanged()
        directory = self.install.control / "local-changes" / uuid.uuid4().hex
        directory.mkdir(parents=True, mode=0o700)
        (directory / "files").mkdir(mode=0o700)
        self.directory = directory
        for name, expected in self.files.items():
            value, content = _read(self.install.root, name)
            if value != expected:
                raise LauncherError("A local file changed during backup. Retry the update.")
            if value["kind"] != "absent":
                blob = _path(directory / "files", name)
                blob.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with blob.open("wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                blob.chmod(0o600)
        index_path = Path(git(self.install.root, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        with (directory / "index").open("wb") as stream:
            stream.write(index_path.read_bytes())
            stream.flush()
            os.fsync(stream.fileno())
        (directory / "index").chmod(0o600)
        write_json(directory / "manifest.json", {
            "schema": 1, "root": str(self.install.root), "before": self.before, "target": self.target,
            "policy": policy, "files": self.files, "index": self.index, "staged": self.staged,
            "index_sha256": hashlib.sha256((directory / "index").read_bytes()).hexdigest(),
        })
        self.unchanged()
        return directory

    @classmethod
    def load(cls, install: Installation, directory: str):
        folder = Path(directory)
        if (folder.is_symlink() or folder.resolve().parent != (install.control / "local-changes").resolve()
                or not re.fullmatch(r"[0-9a-f]{32}", folder.name)):
            raise LauncherError("Invalid local-change backup location. Preserve the recovery journal.")
        record = read_json(folder / "manifest.json")
        if (record.get("schema") != 1 or record.get("root") != str(install.root)
                or not all(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", str(record.get(key, "")))
                           for key in ("before", "target"))
                or not isinstance(record.get("files"), dict) or not isinstance(record.get("index"), dict)
                or not isinstance(record.get("staged"), list)):
            raise LauncherError("Invalid local-change backup manifest.")
        value = cls.__new__(cls)
        value.install, value.before, value.target = install, record["before"], record["target"]
        value.base, value.incoming = _tree(install.root, value.before), _tree(install.root, value.target)
        value.files, value.index, value.staged = record["files"], record["index"], record["staged"]
        value.paths, value.directory = sorted(value.files), folder
        for name in value.paths:
            _path(install.root, name)
            if not isinstance(value.files[name], dict) or value.files[name].get("kind") not in {"file", "symlink", "absent"}:
                raise LauncherError("Invalid local backup file type.")
        return value

    def _restore_source(self, commit: str) -> None:
        entries = _tree(self.install.root, commit)
        indexed = _index(self.install.root)
        tracked = [name for name in self.paths if name in entries or name in indexed]
        if tracked:
            git_bytes(self.install.root, "restore", f"--source={commit}", "--staged", "--worktree",
                      "--pathspec-from-file=-", "--pathspec-file-nul",
                      data=b"\0".join(name.encode("utf-8") for name in tracked) + b"\0")
        for name in self.paths:
            if name not in entries:
                path = _path(self.install.root, name)
                if path.is_dir() and not path.is_symlink():
                    raise LauncherError(f"A directory appeared at {name!r}; recovery needs attention.")
                path.unlink(missing_ok=True)

    def clear(self) -> None:
        self.unchanged()
        self._restore_source(self.before)

    def _write_files(self) -> None:
        assert self.directory is not None
        for name, value in self.files.items():
            path = _path(self.install.root, name)
            if path.is_dir() and not path.is_symlink():
                raise LauncherError(f"A directory appeared at {name!r}; preserve it and resolve the path manually.")
            if value["kind"] == "absent":
                path.unlink(missing_ok=True)
                continue
            digest = value["sha256"]
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise LauncherError("Invalid local backup content identifier.")
            blob = _path(self.directory / "files", name)
            if blob.is_symlink():
                raise LauncherError("Local backup content was replaced by a symlink. Recovery stopped.")
            content = blob.read_bytes()
            if hashlib.sha256(content).hexdigest() != digest:
                raise LauncherError("Local backup content is damaged. Recovery stopped.")
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(prefix=".astra-local-", dir=path.parent)
            staging = Path(temporary)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                if value["kind"] == "symlink":
                    staging.unlink()
                    staging.symlink_to(os.fsdecode(content))
                else:
                    staging.chmod(value["mode"])
                os.replace(staging, path)
            finally:
                staging.unlink(missing_ok=True)

    def apply(self) -> None:
        self.require_owned()
        self._write_files()
        rows = []
        for name in self.staged:
            mode, oid = self.index.get(name, ["0", "0" * len(self.before)])
            rows.append(f"{mode} {oid}\t{name}".encode("utf-8") + b"\0")
        if rows:
            git_bytes(self.install.root, "update-index", "-z", "--index-info", data=b"".join(rows))

    def _matches_commit(self, name: str, current: dict, commit: str, entries: dict) -> bool:
        entry = entries.get(name)
        if not entry:
            return current["kind"] == "absent"
        mode, _ = entry
        if current["kind"] == "absent":
            return False
        content = git_bytes(self.install.root, "cat-file", "--filters", f"{commit}:{name}")
        if hashlib.sha256(content).hexdigest() != current.get("sha256"):
            return False
        if mode == "120000":
            return current["kind"] == "symlink" or os.name == "nt"
        return current["kind"] == "file" and (os.name == "nt" or bool(current["mode"] & 0o111) == (mode == "100755"))

    def require_owned(self) -> None:
        kept_index = dict(self.incoming)
        for name in self.staged:
            if name in self.index:
                kept_index[name] = self.index[name]
            else:
                kept_index.pop(name, None)
        if _index(self.install.root) not in (self.index, self.base, self.incoming, kept_index):
            raise LauncherError("The Git index changed outside the updater. Recovery paused to preserve it.")
        for name, original in self.files.items():
            current, _ = _read(self.install.root, name)
            if (current != original and not self._matches_commit(name, current, self.before, self.base)
                    and not self._matches_commit(name, current, self.target, self.incoming)):
                raise LauncherError(f"Local file {name!r} changed outside the updater. Recovery paused to preserve it.")

    def prepare_recovery(self) -> None:
        self.require_owned()
        self._restore_source(git(self.install.root, "rev-parse", "HEAD"))

    def restore_original(self) -> None:
        assert self.directory is not None
        self.require_owned()
        self._write_files()
        index_path = Path(git(self.install.root, "rev-parse", "--path-format=absolute", "--git-path", "index"))
        lock = index_path.with_name(index_path.name + ".lock")
        original = self.directory / "index"
        if original.is_symlink():
            raise LauncherError("Local index backup was replaced. Recovery stopped.")
        content = original.read_bytes()
        if hashlib.sha256(content).hexdigest() != read_json(self.directory / "manifest.json").get("index_sha256"):
            raise LauncherError("Local index backup is damaged. Recovery stopped.")
        stream = lock.open("xb")
        try:
            with stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(lock, index_path)
        finally:
            lock.unlink(missing_ok=True)
