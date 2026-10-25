"""The person's own data: export it as one zip, or wipe it (``astra data``).

``export`` copies what Astra keeps about the person: the two core memory files, saved memories,
conversations and their search index, scheduled jobs, and learned skills. SQLite files go through SQLite's
backup API, so an export is consistent while Astra runs. Credentials and logins (.env, provider connections,
the WeChat login, browser profiles) are never exported.

``wipe`` deletes the same data and every copy made from it (search and vector indexes, task, event and
approval records, the desktop's history cache and pasted images). It runs only while no other Astra runs,
and only when the person types 清除. Browser profiles (logins inside Astra's own browser) go only with
--browser-data. Settings, models, credentials and logins, the mail cache and activity history stay. In a
source checkout, files tracked by git are never deleted.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agent.launcher.common import LauncherError

CONFIRMATION = "清除"
ARCHIVE_ROOT = "astra-export"
_SQLITE_SUFFIXES = (".db", ".sqlite3")
_SQLITE_SIDES = ("-wal", "-shm", "-journal")
# Locks and markers belong to running processes, not to the person.
_SKIP_NAMES = {".writers", "owners", "scheduler.lock", "active", "last-interaction"}
NOT_EXPORTED = (".env 和其他密钥", "模型和服务商连接", "微信登录", "浏览器登录（Astra 浏览器的配置文件）", "设置",
                "邮件缓存", "活动记录")
KEPT = ("设置和模型", ".env 和其他密钥、服务商连接", "微信登录", "邮件缓存（可以重新同步）",
        "活动记录（在活动页单独清除）", "Hindsight 服务器上的记忆（如果用了，要在那边清）")


@dataclass(frozen=True)
class Item:
    key: str
    label: str
    path: Path
    export: bool
    browser: bool = False

    @property
    def sqlite(self) -> bool:
        return self.path.suffix in _SQLITE_SUFFIXES


def _env_path(name: str, default: Path) -> Path:
    configured = os.environ.get(name, "").strip()
    return Path(configured).expanduser() if configured else default


def items() -> list[Item]:
    """Everything export and wipe act on, resolved the way the runtime resolves each path."""
    from agent.runtime.approval_inbox import approval_db_path
    from agent.runtime.browser_session import browser_db_path
    from agent.runtime.event_stream import event_db_path
    from agent.runtime.learning import default_learning_path
    from agent.runtime.memory import default_memory_path
    from agent.runtime.paths import sessions_dir, state_dir, state_path
    from agent.runtime.session_recall import configured_session_recall_path
    from agent.runtime.skill_provenance import learning_home
    from agent.runtime.skills import default_skills_path
    from agent.runtime.task_store import task_db_path

    memory = default_memory_path()
    recall = configured_session_recall_path()
    skills = default_skills_path()
    browser = browser_db_path()
    result = [
        Item("core-user", "核心记忆：关于你（USER.md）", memory.parent / "memory" / "USER.md", True),
        Item("core-memory", "核心记忆：Astra 的笔记（MEMORY.md）", memory.parent / "memory" / "MEMORY.md", True),
        Item("memory", "已保存的记忆", memory, True),
        Item("sessions", "对话记录", sessions_dir(), True),
        Item("recall", "对话搜索索引", recall, True),
        Item("jobs", "定时任务和结果", state_path("jobs"), True),
        Item("skills", "学到的技能", skills / "learned", True),
        Item("skills-journal", "技能学习记录", learning_home(skills), True),
        Item("learning", "技能学习建议", default_learning_path(), True),
        # Copies made from conversations: wiped with them, not worth exporting.
        Item("feedback", "对话检索反馈", _env_path("ASTRA_CONTEXT_INDEX_FEEDBACK_DB",
                                               recall.with_name("context-index-feedback.db")), False),
        Item("tasks", "任务和步骤记录", task_db_path(), False),
        Item("events", "运行事件", event_db_path(), False),
        Item("approvals", "审批记录", approval_db_path(), False),
        Item("peers", "会话之间的消息", _env_path("ASTRA_PEER_DB", state_path("peers.db")), False),
        Item("channel-chats", "微信和 QQ 的会话对应", state_path("channels", "conversations.json"), False),
        Item("history-cache", "桌面端的历史缓存", _env_path("ASTRA_GUI_HISTORY_CACHE", state_path("gui", "history-index")),
             False),
        Item("attachments", "粘贴过的图片", state_path("gui", "attachments"), False),
        Item("page-snapshots", "页面截图", state_path("gui", "page-snapshots"), False),
        Item("browser-profiles", "Astra 浏览器的登录和 Cookie", browser.parent / "browser-profiles", False, browser=True),
        Item("browser-sessions", "Astra 浏览器的标签页记录", browser, False, browser=True),
    ]
    vectors = os.environ.get("ASTRA_CONTEXT_INDEX_VECTORS_DB", "").strip()
    for path in [Path(vectors).expanduser()] if vectors else sorted(state_dir().glob("context-vectors*.db")):
        result.append(Item(f"vectors:{path.name}", "对话的向量索引", path, False))
    return result


def _tracked(root: Path, path: Path) -> set[Path]:
    """Files under ``path`` that git tracks in a source checkout; they are never deleted."""
    if not (root / ".git").exists() or not path.resolve().is_relative_to(root.resolve()):
        return set()
    try:
        listed = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--", str(path.resolve())],
                                capture_output=True, check=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError("git could not say which files belong to the checkout; nothing here was deleted") from exc
    return {(root / name).resolve() for name in listed.decode().split("\0") if name}


def _guard(path: Path, root: Path) -> None:
    """A misconfigured path must never take the home folder, a disk or the installation with it."""
    target = path.resolve()
    for protected in (Path.home().resolve(), root.resolve(), Path(target.anchor)):
        if target == protected or protected.is_relative_to(target):
            raise OSError(f"{path} holds {protected}; it was not deleted")


def _files(path: Path) -> Iterator[Path]:
    if path.is_symlink() or not path.exists():
        return
    if path.is_file():
        yield path
        return
    for current, folders, names in os.walk(path):
        folders[:] = sorted(name for name in folders if name not in _SKIP_NAMES
                            and not (Path(current) / name).is_symlink())
        for name in sorted(names):
            entry = Path(current) / name
            if name in _SKIP_NAMES or name.endswith(".lock") or name.endswith(_SQLITE_SIDES) or entry.is_symlink():
                continue
            yield entry


def _size(path: Path) -> int:
    total = 0
    for entry in _files(path):
        try:
            total += entry.stat().st_size + sum(Path(f"{entry}{side}").stat().st_size
                                                for side in _SQLITE_SIDES if Path(f"{entry}{side}").exists())
        except OSError:
            pass
    return total


def plan() -> dict:
    """What export and wipe would take, for the person to read before choosing."""
    listed = [{"key": item.key, "label": item.label, "path": str(item.path), "present": item.path.exists(),
               "bytes": _size(item.path), "export": item.export, "browser": item.browser} for item in items()]
    return {"items": listed, "not_exported": list(NOT_EXPORTED), "kept": list(KEPT), "confirmation": CONFIRMATION}


def _copy_sqlite(source: Path, target: Path) -> None:
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10)) as src, \
            closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def export(out: Path, *, now: float | None = None) -> dict:
    out = out.expanduser().resolve()
    if out.suffix != ".zip":
        raise ValueError("导出文件要以 .zip 结尾。")
    if not out.parent.is_dir():
        raise ValueError(f"找不到文件夹：{out.parent}")
    when = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    partial = out.with_name(out.name + ".partial")
    exported = []
    try:
        with tempfile.TemporaryDirectory(prefix="astra-export-") as scratch, \
                zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            os.chmod(partial, 0o600)
            for item in items():
                if not item.export or not item.path.exists():
                    continue
                count = size = 0
                for entry in _files(item.path):
                    relative = entry.name if entry == item.path else entry.relative_to(item.path).as_posix()
                    name = f"{ARCHIVE_ROOT}/{item.key}/{relative}"
                    source = entry
                    if entry.suffix in _SQLITE_SUFFIXES:
                        source = Path(scratch) / f"{count}-{entry.name}"
                        try:
                            _copy_sqlite(entry, source)
                        except sqlite3.DatabaseError:
                            source = entry
                    archive.write(source, name)
                    count += 1
                    size += source.stat().st_size
                exported.append({"key": item.key, "label": item.label, "files": count, "bytes": size})
            archive.writestr(f"{ARCHIVE_ROOT}/manifest.json", json.dumps({
                "format": "astra-export", "version": 1, "created_at": when.isoformat(timespec="seconds"),
                "items": exported, "not_included": list(NOT_EXPORTED),
            }, ensure_ascii=False, indent=2))
        partial.replace(out)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return {"path": str(out), "bytes": out.stat().st_size, "items": exported}


def _others_running(install, *, desktop: bool) -> list[dict]:
    from agent.launcher.locking import active_instances

    # The desktop closes its own sessions and background host first; only its own lease may remain.
    return [item for item in active_instances(install) if not (desktop and item.get("role") == "desktop")]


def _remove(path: Path, tracked: set[Path]) -> int:
    """Delete ``path`` (with SQLite's side files), keeping tracked files; return how many files went."""
    removed = 0
    if path.is_symlink():
        path.unlink()
        return 1
    if path.is_file():
        if path.resolve() in tracked:
            return 0
        path.unlink()
        for side in _SQLITE_SIDES:
            Path(f"{path}{side}").unlink(missing_ok=True)
        return 1
    if not path.is_dir():
        return 0
    if not tracked:
        removed = sum(1 for _ in path.rglob("*") if _.is_file())
        shutil.rmtree(path)
        return removed
    for entry in sorted(path.rglob("*"), key=lambda value: len(value.parts), reverse=True):
        if entry.is_symlink() or entry.is_file():
            if entry.resolve() not in tracked:
                entry.unlink()
                removed += 1
        elif entry.is_dir() and not any(entry.iterdir()):
            entry.rmdir()
    if path.is_dir() and not any(path.iterdir()):
        path.rmdir()
    return removed


def wipe(confirmation: str, *, browser_data: bool = False, desktop: bool = False, root: Path | None = None) -> dict:
    from agent.launcher.installation import discover
    from agent.launcher.locking import exclusive

    if confirmation != CONFIRMATION:
        raise ValueError(f"要清除，请准确输入「{CONFIRMATION}」。")
    install = discover(root)
    removed, failed = [], []
    # Holding the startup lock: no Astra can start while the data goes.
    with exclusive(install, allow_pending=True):
        running = _others_running(install, desktop=desktop)
        if running:
            roles = "、".join(sorted({str(item.get("role") or "Astra") for item in running}))
            raise ValueError(f"先关掉正在运行的 Astra（{roles}），再清除。")
        for item in items():
            if item.browser and not browser_data:
                continue
            try:
                _guard(item.path, install.root)
                count = _remove(item.path, _tracked(install.root, item.path))
            except OSError as exc:
                failed.append({"key": item.key, "label": item.label, "error": str(exc)})
                continue
            if count:
                removed.append({"key": item.key, "label": item.label, "files": count})
    return {"removed": removed, "failed": failed, "kept": list(KEPT)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra data", description=(__doc__ or "").split("\n\n")[0])
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("plan", help="list what export and wipe would take")
    out = commands.add_parser("export", help="save the data as one zip")
    out.add_argument("--out", type=Path, required=True)
    erase = commands.add_parser("wipe", help="delete the data; needs --confirm 清除")
    erase.add_argument("--confirm", default="")
    erase.add_argument("--browser-data", action="store_true", help="also delete Astra's browser profiles")
    erase.add_argument("--desktop", action="store_true", help=argparse.SUPPRESS)
    erase.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.action == "plan":
            value = plan()
        elif args.action == "export":
            value = export(args.out)
        else:
            value = wipe(args.confirm, browser_data=args.browser_data, desktop=args.desktop, root=args.root)
    except (ValueError, OSError, LauncherError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(value, ensure_ascii=False))
    return 1 if value.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
