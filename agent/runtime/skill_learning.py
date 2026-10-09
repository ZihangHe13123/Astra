"""Direct skill learning with ownership checks, a small journal, and undo.

Only learned/ is writable here. The journal lives outside the skill catalog.
There is no scheduler, trial protocol, or automatic quality score.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .instance_lock import InstanceAlreadyRunning, InstanceLock
from .learning_scope import learning_scope, scope_key
from .skill_provenance import AUTO_ORIGIN, learning_home, read_learning_state
from .skills import SkillStore

MAX_TREE_CHARS = 200_000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(files: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".learning-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class LearnedSkills:
    def __init__(self, skills: SkillStore):
        self.skills = skills
        self.root = skills.root.absolute()
        self.home = learning_home(self.root)
        if self.skills.read_allowed and (self.home / "journal.json").exists():
            try:
                with self.locked():
                    pass  # Recover file writes only; never restart a model task.
            except InstanceAlreadyRunning:
                pass  # A live writer owns its own transaction.

    def _state_path(self, path: Path) -> Path:
        if not path.is_relative_to(self.home) or ".." in path.parts:
            raise ValueError("Invalid learning state path")
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise ValueError("Learning state cannot use symlinks")
        return path

    def _path(self, name: str, relative: str = "SKILL.md") -> Path:
        name = self.skills._validate_name(name)
        parts = Path(relative.replace("\\", "/"))
        if (parts.is_absolute() or ".." in parts.parts or any(":" in p for p in parts.parts)
                or (parts.as_posix() != "SKILL.md" and (not parts.parts or parts.parts[0] not in
                    {"references", "templates", "scripts", "assets"}))):
            raise ValueError(
                "Invalid learned skill path: use SKILL.md or a relative path under "
                "references/, templates/, scripts/ or assets/"
            )
        path = self.root / "learned" / name / parts
        # Do not follow a user-provided symlink, including the learned root.
        for parent in (path, *path.parents):
            if parent.is_symlink():
                raise ValueError("Learning does not write through symlinks")
        return path

    def _snapshot(self, name: str) -> dict[str, str]:
        directory = self._path(name).parent
        if not directory.exists():
            return {}
        result: dict[str, str] = {}
        size = 0
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError("Learning does not follow skill symlinks")
            if path.is_file():
                relative = path.relative_to(directory).as_posix()
                self._path(name, relative)
                if path.stat().st_size > MAX_TREE_CHARS * 4:
                    raise ValueError("Skill is too large for safe maintenance")
                result[relative] = path.read_text(encoding="utf-8")
                size += len(result[relative])
                if size > MAX_TREE_CHARS:
                    raise ValueError("Skill is too large for safe maintenance")
        return result

    def _state(self) -> dict[str, Any]:
        return read_learning_state(self.root)

    def _json(self, path: Path, data: Any) -> None:
        _write(self._state_path(path), json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    @contextmanager
    def locked(self) -> Iterator[None]:
        if not self.skills.read_allowed:
            raise PermissionError("Project skills are disabled until the project is trusted")
        for path in (self.home, *self.home.parents):
            if path.is_symlink():
                raise ValueError("Learning state cannot be stored through symlinks")
        with InstanceLock(self.home / "maintenance.lock"):
            self._recover()
            yield

    def _replace(self, name: str, files: dict[str, str]) -> None:
        current = self._snapshot(name)
        for relative, content in files.items():
            if current.get(relative) != content:
                _write(self._path(name, relative), content)
        for relative in current.keys() - files.keys():
            self._path(name, relative).unlink()
        directory = self._path(name).parent
        if directory.exists():
            for path in sorted((p for p in directory.rglob("*") if p.is_dir()), reverse=True):
                if not any(path.iterdir()):
                    path.rmdir()
            if not any(directory.iterdir()):
                directory.rmdir()

    def _recover(self) -> None:
        path = self._state_path(self.home / "journal.json")
        if not path.exists():
            return
        record = json.loads(path.read_text(encoding="utf-8"))
        if not re.fullmatch(r"sl_\d{8}T\d{6}_[0-9a-f]{8}", str(record.get("id", ""))):
            raise ValueError("Invalid learning journal ID")
        if record["status"] == "applied":
            self._json(self.home / "runs" / (record["id"] + ".json"), record)
            path.unlink()
            return
        # A crash can leave a mixture of old/new files. A later user edit is
        # never overwritten, even during recovery of a partial transaction.
        for name, before in record["before"].items():
            after = record["after"][name]
            current = self._snapshot(name)
            for relative in before.keys() | after.keys() | current.keys():
                if current.get(relative) not in (before.get(relative), after.get(relative)):
                    raise ValueError(f"Recovery conflict in {name}/{relative}; original versions are in {path}")
        for name, before in record["before"].items():
            self._replace(name, before)
        self._json(self.home / "index.json", record["state_before"])
        record["status"] = "interrupted"
        self._json(self.home / "runs" / (record["id"] + ".json"), record)
        path.unlink()

    def commit(self, *, kind: str, state: dict, after: dict[str, dict[str, str]],
               actions: list[dict], source: dict | None = None,
               expected: dict[str, dict[str, str]] | None = None) -> dict:
        """Commit under locked(); recover all files together on failure."""
        before = {name: self._snapshot(name) for name in after}
        if expected is not None and before != expected:
            raise ValueError("Skill files changed before commit; nothing was overwritten")
        record = {"id": "sl_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + uuid.uuid4().hex[:8],
                  "time": _now(), "kind": kind, "status": "applying", "actions": actions,
                  "source": source or {}, "before": before, "after": after,
                  "state_before": self._state(), "state_after": state}
        journal = self.home / "journal.json"
        self._json(journal, record)
        try:
            for name, files in after.items():
                if self._snapshot(name) != before[name]:
                    raise ValueError(f"{name} changed during commit; preserving the newer files")
                self._replace(name, files)
            self._json(self.home / "index.json", state)
            record["status"] = "applied"
            self._json(journal, record)
            self._json(self.home / "runs" / (record["id"] + ".json"), record)
            journal.unlink()
        except BaseException:
            self._recover()
            raise
        return record

    def owned(self, name: str, state: dict) -> dict[str, str]:
        meta = state["skills"].get(name)
        if not isinstance(meta, dict) or meta.get("origin") != AUTO_ORIGIN:
            raise ValueError(f"{name} is not a model-owned learned skill; use explicit /skills maintenance")
        files = self._snapshot(name)
        if not files or _digest(files) != meta["digest"]:
            raise ValueError(f"{name} was edited outside learning; preserving your changes")
        header = self.skills._frontmatter(files["SKILL.md"])
        if header.get("pinned", "").lower() in {"true", "1", "yes"}:
            raise ValueError(f"{name} is pinned")
        if meta["scope"] != scope_key(learning_scope()):
            raise ValueError(f"{name} belongs to another workspace/platform")
        if (self.skills._existing_skill_dir(name) or Path()).absolute() != self._path(name).parent:
            raise ValueError(f"{name} has a conflicting skill location")
        return files

    def validate(self, name: str, text: str) -> str:
        text = text.replace("\r\n", "\n").strip() + "\n"
        header = self.skills._frontmatter(text)
        if self.skills._validate_name(header["name"]) != name:
            raise ValueError(f"Frontmatter name must remain {name}; the review cannot rename skills")
        if len(text) > 100_000:
            raise ValueError("Skill is too large")
        # Model-supplied metadata cannot widen the installation/workspace scope.
        end = text.find("\n---", 4)
        lines = [line for line in text[4:end].splitlines()
                 if not line.startswith(("astra_learning_scope:", "pinned:"))]
        return "---\n" + "\n".join(lines) + "\nastra_learning_scope: " + scope_key(learning_scope()) + text[end:]

    def manage(self, action: str, name: str, *, content: str = "", file_path: str = "SKILL.md",
               old_string: str = "", new_string: str = "", source: dict | None = None) -> dict:
        name = self.skills._validate_name(name)
        file_path = Path(file_path.replace("\\", "/")).as_posix()
        with self.locked():
            state = self._state()
            if action == "create":
                if self.skills._existing_skill_dir(name) is not None or self._snapshot(name):
                    raise ValueError(f"Skill already exists: {name}; read it and patch if model-owned")
                files = {"SKILL.md": self.validate(name, content)}
                original = {}
                sources = []
            else:
                files = self.owned(name, state)
                original = dict(files)
                sources = state["skills"][name]["sources"]
                self._path(name, file_path)
                if action == "patch":
                    if not old_string or old_string == new_string:
                        raise ValueError("Patch requires distinct non-empty old_string and new_string")
                    if file_path not in files:
                        raise ValueError(f"Skill file not found: {name}/{file_path}")
                    count = files[file_path].count(old_string)
                    if count != 1:
                        raise ValueError(f"old_string must match exactly once (found {count})")
                    content = files[file_path].replace(old_string, new_string, 1)
                elif action != "write_file" or file_path == "SKILL.md":
                    raise ValueError("Use create/patch for SKILL.md; write_file is for supporting files")
                files[file_path] = self.validate(name, content) if file_path == "SKILL.md" else content
            if sum(map(len, files.values())) > MAX_TREE_CHARS:
                raise ValueError("Skill is too large for safe maintenance")
            state["skills"][name] = {"origin": AUTO_ORIGIN, "digest": _digest(files), "scope": scope_key(learning_scope()),
                                     "sources": (sources + [source or {"origin": "skill_manage"}])[-8:]}
            record = self.commit(kind="save", state=state, after={name: files},
                                 actions=[{"action": action, "name": name, "file": file_path}], source=source,
                                 expected={name: original})
            return {"name": name, "status": "saved", "origin": AUTO_ORIGIN, "category": "learned", "run_id": record["id"],
                    "notice": "Available through skill_view now. Saved experience is not independently verified."}

    def history(self, run_id: str = "", limit: int = 20) -> list[dict]:
        if run_id and not re.fullmatch(r"sl_\d{8}T\d{6}_[0-9a-f]{8}", run_id):
            raise ValueError("Invalid learning run ID")
        paths = ([self.home / "runs" / (run_id + ".json")] if run_id else
                 sorted((self.home / "runs").glob("sl_*.json"), reverse=True)[:limit])
        return [json.loads(self._state_path(path).read_text(encoding="utf-8")) for path in paths]

    def undo(self, run_id: str) -> dict:
        with self.locked():
            record = self.history(run_id)[0]
            if record["status"] != "applied":
                raise ValueError("Only completed learning changes can be undone")
            state = self._state()
            legacy_before = record["state_before"].get("legacy_migrations", {})
            legacy_after = record["state_after"].get("legacy_migrations", {})
            changed_legacy = {key for key in legacy_before.keys() | legacy_after.keys()
                              if legacy_before.get(key) != legacy_after.get(key)}
            for key in changed_legacy:
                if state.get("legacy_migrations", {}).get(key) != legacy_after.get(key):
                    raise ValueError("Undo conflict: legacy migration record changed; nothing was overwritten")
            for name, expected in record["after"].items():
                if self._snapshot(name) != expected or state["skills"].get(name) != record["state_after"]["skills"].get(name):
                    raise ValueError(f"Undo conflict: {name} changed after this run; nothing was overwritten")
            for name in record["before"]:
                previous = record["state_before"]["skills"].get(name)
                if previous is None:
                    state["skills"].pop(name, None)
                else:
                    state["skills"][name] = copy.deepcopy(previous)
            for key in changed_legacy:
                if key in legacy_before:
                    state.setdefault("legacy_migrations", {})[key] = copy.deepcopy(legacy_before[key])
                else:
                    state.get("legacy_migrations", {}).pop(key, None)
            # Undo does not rewind other sessions' progress or unrelated skills.
            return self.commit(kind="undo", state=state, after=record["before"],
                               actions=[{"action": "undo", "run_id": run_id}], expected=record["after"])
