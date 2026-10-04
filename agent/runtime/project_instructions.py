"""Budgeted project guidance with hierarchical and path-scoped loading."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path

from .project_trust import ProjectTrust

_EXCLUDED = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build"}


def tool_paths(args: dict) -> set[str]:
    """Extract bounded local path hints without evaluating shell text."""
    paths = {value for key in ("path", "file_path", "directory", "root", "workdir", "cwd", "output_path")
             if isinstance(value := args.get(key), str) and value.strip()}
    values = args.get("paths")
    if isinstance(values, list):
        paths.update(value for value in values[:128] if isinstance(value, str))
    command = args.get("command")
    if isinstance(command, str) and len(command) <= 16_384:
        try:
            tokens = shlex.split(command, posix=os.name != "nt")
        except ValueError:
            tokens = []
        paths.update(tokens[i + 1].strip("\"'") for i, token in enumerate(tokens[:-1]) if token in {"cd", "pushd"})
    return paths


@dataclass(frozen=True)
class PathRule:
    path: Path
    patterns: tuple[str, ...]
    body: str


class ProjectInstructions:
    def __init__(
        self,
        workdir: str | Path,
        *,
        max_bytes: int | None = None,
        trusted: bool | None = None,
        snapshot: object = None,
    ):
        self.workdir = Path(workdir).expanduser().resolve()
        self.root = self._project_root(self.workdir)
        self.max_bytes = max_bytes or self._positive_env("PROJECT_INSTRUCTIONS_MAX_BYTES", 32 * 1024)
        decision = ProjectTrust.for_path(self.workdir)
        self.trusted = decision.trusted if trusted is None else bool(trusted)
        self._trust_override = trusted
        self.base_prompt = ""
        self.rules: list[PathRule] = []
        self.warnings: list[str] = []
        self._records: dict[str, str] = {}
        self._base_paths: set[str] = set()
        self._seen_dirs: set[str] = set()
        self._digests: set[str] = set()
        self._source_digests: dict[str, str] = {}
        self._used_bytes = 0
        if not self._restore(snapshot):
            self.reload()

    @staticmethod
    def _positive_env(name: str, default: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError:
            return default
        return value if value > 0 else default

    @staticmethod
    def _project_root(workdir: Path) -> Path:
        for directory in (workdir, *workdir.parents):
            if (directory / ".git").exists():
                return directory
        return workdir

    def _directories(self) -> list[Path]:
        try:
            relative = self.workdir.relative_to(self.root)
        except ValueError:
            return [self.workdir]
        directories = [self.root]
        current = self.root
        for part in relative.parts:
            current = current / part
            directories.append(current)
        return directories

    @staticmethod
    def _read_bounded(path: Path, remaining: int) -> str:
        if remaining <= 0 or not path.is_file():
            return ""
        try:
            with path.open("rb") as handle:
                raw = handle.read(remaining)
            return raw.decode("utf-8", errors="ignore")
        except OSError:
            return ""

    @staticmethod
    def _parse_rule(path: Path, remaining: int) -> PathRule | None:
        content = ProjectInstructions._read_bounded(path, remaining).replace("\r\n", "\n")
        if not content.startswith("---\n"):
            return None
        end = content.find("\n---", 4)
        if end < 0:
            return None
        frontmatter = content[4:end]
        body = content[end + 4:].lstrip("\r\n")
        patterns: list[str] = []
        in_paths = False
        for line in frontmatter.splitlines():
            stripped = line.strip()
            if re.match(r"^paths\s*:\s*$", stripped):
                in_paths = True
                continue
            inline = re.match(r"^paths\s*:\s*\[(.*)]\s*$", stripped)
            if inline:
                patterns.extend(
                    item.strip().strip("\"'")
                    for item in inline.group(1).split(",")
                    if item.strip()
                )
                in_paths = False
                continue
            if in_paths and stripped.startswith("-"):
                value = stripped[1:].strip().strip("\"'")
                if value:
                    patterns.append(value)
            elif stripped and not stripped.startswith("#"):
                in_paths = False
        if not patterns or not body.strip():
            return None
        return PathRule(path=path, patterns=tuple(patterns[:128]), body=body.strip())

    def reload(self) -> None:
        self.base_prompt = ""
        self.rules = []
        self.warnings = []
        self._records = {}
        self._base_paths = set()
        self._seen_dirs = set()
        self._digests = set()
        self._source_digests = {}
        self._used_bytes = 0
        if self.trusted:
            for directory in self._directories():
                self._load_directory(directory, base=True)
            self.base_prompt = self._render(base=True)

    def _safe_path(self, path: Path) -> bool:
        try:
            path.relative_to(self.root)
            path.resolve().relative_to(self.root)
            return True
        except (OSError, ValueError, RuntimeError):
            return False

    def _label(self, relative: str) -> str:
        return f"## Project guidance: {self.root / relative}\n"

    def _render(self, *, base: bool) -> str:
        return "\n\n".join(self._label(path) + body for path, body in self._records.items()
                            if (path in self._base_paths) == base)

    @property
    def discovered_prompt(self) -> str:
        return self._render(base=False)

    @property
    def sources(self) -> list[str]:
        return [*self._records, *(rule.path.relative_to(self.root).as_posix() for rule in self.rules)]

    def _load_directory(self, directory: Path, *, base: bool = False) -> None:
        relative_dir = directory.relative_to(self.root).as_posix()
        if relative_dir in self._seen_dirs or not self._safe_path(directory):
            return
        if len(self._seen_dirs) >= 2048:
            if "Project directory discovery limit reached." not in self.warnings:
                self.warnings.append("Project directory discovery limit reached.")
            return
        self._seen_dirs.add(relative_dir)
        selected = directory / "AGENTS.override.md"
        if not selected.is_file():
            selected = directory / "AGENTS.md"
        if self._safe_path(selected) and selected.is_file():
            relative = selected.relative_to(self.root).as_posix()
            available = self.max_bytes - self._used_bytes - len(self._label(relative).encode()) - 2
            if available > 0:
                try:
                    target = selected.resolve(strict=True)
                    size = target.stat().st_size
                except (OSError, RuntimeError):
                    return
                if not self._safe_path(target):
                    return
                content = self._read_bounded(target, self.max_bytes + 1).strip()
                digest = hashlib.sha256(content.encode()).hexdigest()
                if content and digest not in self._digests:
                    if size > available:
                        self.warnings.append(f"Project guidance truncated: {relative}")
                    content = content.encode()[:available].decode("utf-8", errors="ignore").strip()
                    self._records[relative] = content
                    self._source_digests[relative] = digest
                    if base:
                        self._base_paths.add(relative)
                    self._digests.add(digest)
                    self._used_bytes += len((self._label(relative) + content).encode()) + 2
            else:
                self.warnings.append(f"Project guidance budget exhausted: {relative}")
        for name in (".astra", ".agents"):
            rules_dir = directory / name / "rules"
            if not self._safe_path(rules_dir) or not rules_dir.is_dir():
                continue
            try:
                for path in sorted(rules_dir.rglob("*.md")):
                    if not self._safe_path(path):
                        continue
                    remaining = self.max_bytes - self._used_bytes
                    target = path.resolve(strict=True)
                    if target.stat().st_size > remaining:
                        self.warnings.append(f"Project rule exceeds remaining budget: {path.relative_to(self.root)}")
                        continue
                    rule = self._parse_rule(target, remaining)
                    if rule is None:
                        continue
                    rule = PathRule(path, rule.patterns, rule.body)
                    cost = self._rule_cost(rule)
                    if cost > remaining:
                        self.warnings.append(f"Project rule exceeds remaining budget: {path.relative_to(self.root)}")
                        continue
                    self.rules.append(rule)
                    self._used_bytes += cost
                    if self._used_bytes >= self.max_bytes:
                        break
            except OSError:
                continue

    def discover(self, paths: set[str]) -> str:
        """Freeze regional instructions on first access, without changing the base."""
        if self._trust_override is None:
            self.trusted = ProjectTrust.for_path(self.workdir).trusted
        if not self.trusted:
            return ""
        previous = set(self._records)
        for value in sorted(paths)[:128]:
            if not value or "://" in value:
                continue
            path = Path(value).expanduser()
            path = path if path.is_absolute() else self.workdir / path
            if not self._safe_path(path):
                continue
            directory = path if path.is_dir() else path.parent
            try:
                parts = directory.relative_to(self.root).parts
            except ValueError:
                continue
            if any(part in _EXCLUDED for part in parts):
                continue
            current = self.root
            for part in parts[:64]:
                current /= part
                if (current / ".git").exists():
                    break  # a nested checkout is a different project
                self._load_directory(current)
        return "\n\n".join(self._label(path) + body for path, body in self._records.items() if path not in previous)

    def discover_tool_call(self, arguments: object) -> str:
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
        except (ValueError, TypeError):
            return ""
        return self.discover(tool_paths(args)) if isinstance(args, dict) else ""

    def snapshot(self) -> dict:
        return {"version": 1, "workdir": str(self.workdir), "root": str(self.root),
                "records": [{"path": path, "body": body, "base": path in self._base_paths,
                             "content_digest": self._source_digests[path]}
                            for path, body in self._records.items()],
                "seen": sorted(self._seen_dirs), "warnings": list(self.warnings[-128:]),
                "rules": [{"path": rule.path.relative_to(self.root).as_posix(),
                           "patterns": list(rule.patterns), "body": rule.body} for rule in self.rules]}

    @staticmethod
    def _rule_cost(rule: PathRule) -> int:
        return (len(f"## Path-scoped project rule: {rule.path}\n{rule.body}".encode()) + 2
                + sum(len(pattern.encode()) + 2 for pattern in rule.patterns))

    def _restore(self, snapshot: object) -> bool:
        if not isinstance(snapshot, dict) or snapshot.get("version") != 1:
            return False
        if snapshot.get("workdir") != str(self.workdir) or snapshot.get("root") != str(self.root):
            return False
        if not self.trusted:
            return True  # a saved snapshot never grants current trust
        try:
            records, rules, seen = snapshot["records"], snapshot["rules"], snapshot["seen"]
            if not all(isinstance(items, list) and len(items) <= 2048 for items in (records, rules, seen)):
                return False
            for record in records:
                path, body = record["path"], record["body"]
                if (not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts
                        or Path(path).name not in {"AGENTS.md", "AGENTS.override.md"}
                        or not self._safe_path(self.root / path) or not isinstance(body, str)):
                    raise ValueError("invalid guidance record")
                self._records[path] = body
                if record.get("base") is True:
                    self._base_paths.add(path)
                digest = record.get("content_digest", hashlib.sha256(body.encode()).hexdigest())
                if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                    raise ValueError("invalid guidance digest")
                self._source_digests[path] = digest
                self._digests.add(digest)
                self._used_bytes += len((self._label(path) + body).encode()) + 2
            for rule in rules:
                path, body, patterns = rule["path"], rule["body"], rule["patterns"]
                if (not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts
                        or not self._safe_path(self.root / path) or not isinstance(body, str)
                        or not isinstance(patterns, list) or len(patterns) > 128
                        or not all(isinstance(pattern, str) for pattern in patterns)):
                    raise ValueError("invalid project rule")
                restored_rule = PathRule(self.root / path, tuple(patterns), body)
                self.rules.append(restored_rule)
                self._used_bytes += self._rule_cost(restored_rule)
            if self._used_bytes > self.max_bytes:
                raise ValueError("snapshot exceeds guidance budget")
            if not all(isinstance(path, str) and not Path(path).is_absolute() and ".." not in Path(path).parts for path in seen):
                raise ValueError("invalid discovery directory")
            self._seen_dirs = set(seen)
            warnings = snapshot.get("warnings", [])
            self.warnings = [str(item)[:512] for item in warnings[-128:]] if isinstance(warnings, list) else []
            self.base_prompt = self._render(base=True)
            return True
        except (KeyError, TypeError, ValueError, OSError, RuntimeError):
            return False

    def rules_for(self, paths: set[str]) -> str:
        if not paths or not self.rules:
            return ""
        normalized: set[str] = set()
        for path in paths:
            value = str(path).replace("\\", "/")
            while value.startswith("./"):
                value = value[2:]
            normalized.add(value)
        blocks: list[str] = []
        seen: set[Path] = set()
        for rule in self.rules:
            if rule.path in seen:
                continue
            if any(fnmatch.fnmatch(path, pattern) for path in normalized for pattern in rule.patterns):
                seen.add(rule.path)
                blocks.append(f"## Path-scoped project rule: {rule.path}\n{rule.body}")
        return "\n\n".join(blocks)
