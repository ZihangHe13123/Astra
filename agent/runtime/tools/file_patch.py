"""Parse and apply a small, deterministic multi-file patch protocol."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable


class PatchError(ValueError):
    pass


class PatchFormatError(PatchError):
    """The patch text itself is malformed; no file was consulted."""


@dataclass(frozen=True)
class PatchHunk:
    lines: tuple[str, ...]


@dataclass(frozen=True)
class PatchOperation:
    kind: str
    path: str
    content: str = ""
    hunks: tuple[PatchHunk, ...] = field(default_factory=tuple)


def _safe_relative_path(raw: str) -> str:
    value = raw.strip().replace("\\", "/")
    candidate = Path(value)
    if not value or candidate.is_absolute() or any(part == ".." for part in candidate.parts):
        raise PatchError(f"patch paths must be workspace-relative without '..': {raw}")
    return value


def _normalize_patch_format(patch: str) -> str:
    """Validate the single patch protocol exposed by the tool.

    A previous best-effort unified-diff converter silently misparsed
    ``diff --git`` headers and ``/dev/null`` add/delete operations. Failing
    early is safer than applying a different operation than the caller asked
    for. The public tool schema documents the supported ``***`` protocol.
    """
    if patch.startswith("*** Begin Patch"):
        return patch
    raise PatchFormatError(
        "apply_patch accepts the Codex-style '*** Begin Patch' protocol, "
        "not standard unified diff; convert ---/+++ headers to "
        "*** Add File, *** Update File, or *** Delete File directives"
    )


def parse_patch(patch: str) -> list[PatchOperation]:
    patch = _normalize_patch_format(patch)
    lines = patch.replace("\r\n", "\n").split("\n")
    if not lines or lines[0] != "*** Begin Patch":
        raise PatchFormatError("patch must start with '*** Begin Patch'")
    if lines[-1] == "":
        lines.pop()
    if not lines or lines[-1] != "*** End Patch":
        raise PatchFormatError("patch must end with '*** End Patch'")

    operations: list[PatchOperation] = []
    index = 1
    while index < len(lines) - 1:
        header = lines[index]
        index += 1
        if header.startswith("*** Add File: "):
            path = _safe_relative_path(header[len("*** Add File: "):])
            added: list[str] = []
            while index < len(lines) - 1 and not lines[index].startswith("*** "):
                line = lines[index]
                if not line.startswith("+"):
                    raise PatchFormatError(f"add-file lines must start with '+': {path}")
                added.append(line[1:])
                index += 1
            operations.append(PatchOperation("add", path, "\n".join(added) + "\n"))
            continue

        if header.startswith("*** Delete File: "):
            path = _safe_relative_path(header[len("*** Delete File: "):])
            operations.append(PatchOperation("delete", path))
            continue

        if header.startswith("*** Update File: "):
            path = _safe_relative_path(header[len("*** Update File: "):])
            hunks: list[PatchHunk] = []
            current: list[str] | None = None
            while index < len(lines) - 1 and not lines[index].startswith("*** "):
                line = lines[index]
                if line.startswith("@@"):
                    if current is not None:
                        hunks.append(PatchHunk(tuple(current)))
                    current = []
                elif line == r"\ No newline at end of file":
                    pass
                elif current is None:
                    raise PatchFormatError(f"update-file content must start with '@@': {path}")
                elif line[:1] in {" ", "+", "-"}:
                    current.append(line)
                else:
                    raise PatchFormatError(f"invalid hunk line for {path}: {line}")
                index += 1
            if current is not None:
                hunks.append(PatchHunk(tuple(current)))
            if not hunks:
                raise PatchFormatError(f"update file has no hunks: {path}")
            operations.append(PatchOperation("update", path, hunks=tuple(hunks)))
            continue

        raise PatchFormatError(f"unknown patch directive: {header}")

    if not operations:
        raise PatchFormatError("patch contains no operations")
    paths = [operation.path for operation in operations]
    if len(paths) != len(set(paths)):
        raise PatchFormatError("each path may appear only once per patch")
    return operations


def patch_summary(operations: list[PatchOperation]) -> dict:
    additions = 0
    deletions = 0
    for operation in operations:
        if operation.kind == "add":
            additions += len(operation.content.splitlines())
        elif operation.kind == "delete":
            deletions += 1
        else:
            for hunk in operation.hunks:
                additions += sum(1 for line in hunk.lines if line.startswith("+"))
                deletions += sum(1 for line in hunk.lines if line.startswith("-"))
    return {
        "files": len(operations),
        "additions": additions,
        "deletions": deletions,
        "paths": [operation.path for operation in operations],
        "operations": [operation.kind for operation in operations],
    }


def _decode_utf8(path: Path) -> tuple[str, bool, str, str]:
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    has_bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PatchError(f"file is not valid UTF-8: {path}: {exc}") from exc
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), has_bom, newline, digest


def _whitespace_normalize(text: str) -> str:
    """Strip trailing whitespace from each line for fuzzy matching."""
    return "\n".join(line.rstrip() for line in text.split("\n"))


def _apply_hunks(path: Path, content: str, hunks: tuple[PatchHunk, ...]) -> str:
    def _find_and_replace(updated: str, old_block: str, new_block: str, number: int) -> str:
        count = updated.count(old_block)
        if count == 1:
            return updated.replace(old_block, new_block, 1)
        if count > 1:
            raise PatchError(f"hunk {number} matched {count} places in {path}; add more context")
        # ── whitespace-tolerant fallback ──
        norm_block = _whitespace_normalize(old_block)
        norm_updated = _whitespace_normalize(updated)
        # Build a mapping from normalized position back to original slices.
        # Split both into lines, find where norm_block starts in norm_updated.
        updated_lines = updated.split("\n")
        norm_updated_lines = norm_updated.split("\n")
        norm_block_lines = norm_block.split("\n")
        match_count = 0
        match_start = -1
        for i in range(len(norm_updated_lines) - len(norm_block_lines) + 1):
            if norm_updated_lines[i:i + len(norm_block_lines)] == norm_block_lines:
                match_count += 1
                match_start = i
        if match_count == 0:
            raise PatchError(f"hunk {number} did not match {path}; read the file and regenerate the patch")
        if match_count > 1:
            raise PatchError(f"hunk {number} matched {match_count} places (whitespace-tolerant) in {path}; add more context")
        # Reconstruct: original lines before match + new_block + original lines after matched region
        match_end = match_start + len(norm_block_lines)
        # Keep every other line as it is, including empty ones: dropping an
        # empty "before" or "after" loses a leading blank line or the final newline.
        return "\n".join([*updated_lines[:match_start], new_block, *updated_lines[match_end:]])

    updated = content
    for number, hunk in enumerate(hunks, start=1):
        old_lines = [line[1:] for line in hunk.lines if not line.startswith("+")]
        new_lines = [line[1:] for line in hunk.lines if not line.startswith("-")]
        old_block = "\n".join(old_lines)
        new_block = "\n".join(new_lines)
        if not old_block:
            raise PatchFormatError(f"hunk {number} has no context or removed text: {path}")
        updated = _find_and_replace(updated, old_block, new_block, number)
    return updated


def apply_patch(
    patch: str,
    resolve: Callable[[str], Path],
    *,
    dry_run: bool = False,
) -> str:
    operations = parse_patch(patch)
    planned: list[dict] = []
    for operation in operations:
        target = resolve(operation.path)
        if operation.kind == "add":
            if target.exists():
                raise PatchError(f"add target already exists: {target}")
            payload = operation.content.encode("utf-8")
            planned.append({
                "operation": operation,
                "target": target,
                "payload": payload,
                "before_sha256": "",
            })
            continue
        if not target.exists() or not target.is_file():
            raise PatchError(f"patch target is not a file: {target}")
        content, has_bom, newline, before_sha256 = _decode_utf8(target)
        if operation.kind == "delete":
            planned.append({
                "operation": operation,
                "target": target,
                "payload": None,
                "before_sha256": before_sha256,
            })
            continue
        updated = _apply_hunks(target, content, operation.hunks)
        if newline == "\r\n":
            updated = updated.replace("\n", "\r\n")
        payload = updated.encode("utf-8")
        if has_bom:
            payload = b"\xef\xbb\xbf" + payload
        planned.append({
            "operation": operation,
            "target": target,
            "payload": payload,
            "before_sha256": before_sha256,
        })

    summary = patch_summary(operations)
    if dry_run:
        return json.dumps({**summary, "status": "dry_run"}, ensure_ascii=False)

    for item in planned:
        target = item["target"]
        operation = item["operation"]
        if operation.kind == "add":
            if target.exists():
                raise PatchError(f"add target appeared after preflight: {target}")
            continue
        current_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        if current_sha256 != item["before_sha256"]:
            raise PatchError(f"target changed after preflight: {target}")

    staged: list[tuple[dict, Path]] = []
    backups: list[tuple[Path, Path]] = []
    committed_targets: list[Path] = []
    try:
        for item in planned:
            payload = item["payload"]
            if payload is None:
                continue
            target = item["target"]
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                prefix=f".{target.name}.agent-patch-",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            staged.append((item, temporary))

        for item in planned:
            target = item["target"]
            if not target.exists():
                continue
            backup = target.with_name(f".{target.name}.agent-backup-{uuid.uuid4().hex}.tmp")
            os.replace(target, backup)
            backups.append((target, backup))

        for item, temporary in staged:
            target = item["target"]
            os.replace(temporary, target)
            committed_targets.append(target)
    except Exception:
        for target in committed_targets:
            target.unlink(missing_ok=True)
        for target, backup in reversed(backups):
            if backup.exists():
                os.replace(backup, target)
        raise
    finally:
        for _, temporary in staged:
            temporary.unlink(missing_ok=True)

    for _, backup in backups:
        backup.unlink(missing_ok=True)

    results = []
    for item in planned:
        target = item["target"]
        payload = item["payload"]
        results.append({
            "path": str(target),
            "operation": item["operation"].kind,
            "before_sha256": item["before_sha256"],
            "after_sha256": hashlib.sha256(payload).hexdigest() if payload is not None else "",
            "bytes": len(payload) if payload is not None else 0,
        })
    return json.dumps({**summary, "status": "applied", "results": results}, ensure_ascii=False)
