"""Exact-byte distribution manifests, independent of local source installations.

Hashes detect corruption; trust in a release must come from an independently
obtained manifest digest or a separately verified publisher signature.
"""
from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path, PurePosixPath

from .common import LauncherError


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def resources_for(application: Path) -> Path:
    return application / ("Contents/Resources" if application.suffix == ".app" else "resources")


def _entries(root: Path) -> dict:
    root = root.resolve(strict=True)
    entries = {}
    for directory, folders, files in os.walk(root, followlinks=False):
        for name in sorted(folders + files):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                link = os.readlink(path)
                if Path(link).is_absolute() or not path.resolve().is_relative_to(root) or not path.exists():
                    raise LauncherError(f"Unsafe or broken distribution symlink: {relative}")
                entries[relative] = {"symlink": link}
            elif stat.S_ISREG(info.st_mode):
                entries[relative] = {"sha256": sha256(path), "size": info.st_size,
                                     "mode": stat.S_IMODE(info.st_mode)}
            elif not stat.S_ISDIR(info.st_mode):
                raise LauncherError(f"Unsupported distribution entry: {relative}")
    return entries


def seal_tree(root: Path, *, version: str, target: str, **components) -> dict:
    return {"schema": 1, "distribution": "astra-desktop", "version": version, "target": target,
            "components": components, "files": _entries(root)}


def verify_tree(root: Path, manifest: dict, *, target: str | None = None) -> None:
    if (manifest.get("schema") != 1 or manifest.get("distribution") != "astra-desktop"
            or not isinstance(manifest.get("files"), dict) or not manifest["files"]):
        raise LauncherError("Unsupported or empty desktop manifest.")
    if target and target != manifest.get("target"):
        raise LauncherError("Desktop release target does not match this platform/architecture.")
    for name, value in manifest["files"].items():
        parts = PurePosixPath(name).parts
        if (not name or "\\" in name or PurePosixPath(name).is_absolute() or ".." in parts
                or ":" in name or name != PurePosixPath(name).as_posix() or not isinstance(value, dict)):
            raise LauncherError("Invalid desktop manifest path.")
        if "symlink" not in value and not re.fullmatch(r"[0-9a-f]{64}", str(value.get("sha256", ""))):
            raise LauncherError("Invalid desktop manifest hash.")
    current = _entries(root)
    if current.keys() != manifest["files"].keys():
        raise LauncherError("Unexpected or missing desktop distribution files.")
    for name, value in manifest["files"].items():
        actual = current[name]
        if actual != value:
            mismatch = next((key for key in ("sha256", "mode", "size", "symlink")
                             if actual.get(key) != value.get(key)), "entry")
            raise LauncherError(f"Desktop file {'hash' if mismatch == 'sha256' else mismatch} mismatch: {name}")
