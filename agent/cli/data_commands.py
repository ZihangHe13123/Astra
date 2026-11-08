"""The person's own data: export it as one zip, or wipe it (``astra data``).

``export`` copies what Astra keeps about the person: the two core memory files, saved memories,
conversations and their search index, scheduled jobs, learned skills, and the files the personal thread wrote
in its own folder. SQLite files go through SQLite's backup API, so an export is consistent while Astra runs.
Credentials and logins (.env, provider connections, the WeChat login, browser profiles, paired phones) are
never exported, nor is any .env file or file named like a key inside the exported folders.

``wipe`` deletes the same data and every copy made from it (search and vector indexes, task, event and
approval records, the desktop's history cache and pasted images, photos and files sent from a phone, the edit
snapshots and caches in the personal folder, speech made for a phone and the phone's audit log). It runs only
while no other Astra runs, and only when the person types 清除. The files the personal thread wrote, its work
like any project's, go only with --personal-files, and browser profiles (logins inside Astra's own browser)
only with --browser-data. Settings, models, credentials and logins (paired phones among them), the mail cache
and activity history stay. In a source checkout, files tracked by git are never deleted.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import stat
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
# Never exported, wherever they sit (the personal folder is a workspace): .env files, and files named or
# shaped like credentials, the same ones a channel never sends (agent/channels/tools.py).
_CREDENTIAL_NAMES = {"credentials", "credentials.json", "id_dsa", "id_ecdsa", "id_ed25519", "id_rsa", "cookies",
                     "login data"}
_CREDENTIAL_SUFFIXES = {".key", ".pem", ".pfx", ".p12"}
NOT_EXPORTED = (".env 和其他密钥", "模型和服务商连接", "微信登录", "浏览器登录（Astra 浏览器的配置文件）", "配对的手机",
                "设置", "邮件缓存", "活动记录")
PERSONAL_LABEL = "个人对话写的文件"
PERSONAL_KEPT = f"{PERSONAL_LABEL}（选了一起清除才删）"
KEPT = ("设置和模型", ".env 和其他密钥（个人对话文件夹里的也保留）、服务商连接", "微信登录", "配对的手机（在设置里移除）", "邮件缓存（可以重新同步）",
        "活动记录（在活动页单独清除）", "Hindsight 服务器上的记忆（如果用了，要在那边清）")


@dataclass(frozen=True)
class Item:
    key: str
    label: str
    path: Path
    export: bool
    browser: bool = False
    # The personal thread's own work files: wiped only when the person chooses it too.
    personal: bool = False
    # Keys among the files (.env, id_rsa, *.pem, as export leaves out) stay when the rest goes.
    spare_keys: bool = False
    # Folders directly inside ``path`` that are items of their own: export and sizes leave them to those.
    apart: tuple[str, ...] = ()

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
    # As configured, before any link in it is resolved away, so a link is seen and not followed.
    browser = _env_path("AGENT_BROWSER_DB", browser_db_path())
    # The personal thread's own workspace (the desktop's <ASTRA_HOME>/personal, specs/personal-thread.md D6).
    personal = state_path("personal")
    result = [
        Item("core-user", "核心记忆：关于你（USER.md）", memory.parent / "memory" / "USER.md", True),
        Item("core-memory", "核心记忆：Astra 的笔记（MEMORY.md）", memory.parent / "memory" / "MEMORY.md", True),
        Item("memory", "已保存的记忆", memory, True),
        Item("sessions", "对话记录", _env_path("AGENT_SESSION_DIR", sessions_dir()), True),
        Item("recall", "对话搜索索引", recall, True),
        Item("jobs", "定时任务和结果", state_path("jobs"), True),
        Item("skills", "学到的技能", skills / "learned", True),
        Item("skills-journal", "技能学习记录", learning_home(skills), True),
        Item("learning", "技能学习建议", default_learning_path(), True),
        Item("personal", PERSONAL_LABEL, personal, True, apart=("uploads", ".astra"), personal=True, spare_keys=True),
        # Copies made from conversations: wiped with them, not worth exporting.
        Item("feedback", "对话检索反馈", _env_path("ASTRA_CONTEXT_INDEX_FEEDBACK_DB",
                                               recall.with_name("context-index-feedback.db")), False),
        Item("tasks", "任务和步骤记录", _env_path("AGENT_TASK_DB", task_db_path()), False),
        Item("events", "运行事件", event_db_path(), False),
        Item("approvals", "审批记录", approval_db_path(), False),
        Item("peers", "会话之间的消息", _env_path("ASTRA_PEER_DB", state_path("peers.db")), False),
        Item("channel-chats", "微信和 QQ 的会话对应", state_path("channels", "conversations.json"), False),
        Item("history-cache", "桌面端的历史缓存", _env_path("ASTRA_GUI_HISTORY_CACHE", state_path("gui", "history-index")),
             False),
        Item("attachments", "粘贴过的图片", state_path("gui", "attachments"), False),
        Item("page-snapshots", "页面截图", state_path("gui", "page-snapshots"), False),
        # Copies of what the phone keeps: the originals stay on it.
        Item("phone-uploads", "手机发来的照片和文件", personal / "uploads", False),
        # Snapshots taken before the personal thread changed a file, and its image cache.
        Item("personal-copies", "个人对话改文件前留的副本", personal / ".astra", False),
        Item("phone-audio", "读给手机听的语音", _env_path("ASTRA_PHONE_AUDIO_DIR", state_path("gui", "phone-audio")), False),
        # Which phone did what, with its commands and the names of the files it sent.
        Item("phone-audit", "手机的操作记录", state_path("gui", "phone-audit.jsonl"), False),
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


def _is_link(path: Path) -> bool:
    if path.is_symlink():
        return True
    if sys.platform == "win32":
        # A junction redirects a folder like a link, and Python 3.11 does not call it one.
        try:
            return os.lstat(path).st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
        except OSError:
            return False
    return False


def _system_link(path: Path) -> bool:
    """A link of the system's own layout (macOS /tmp, /var and /etc, Linux /bin and /lib): owned by root, in a
    folder owned by root, so neither the person nor a program of theirs made it or can change it. Windows has
    no such links in the places Astra keeps data, so there none counts."""
    if sys.platform == "win32":
        return False
    try:
        return os.lstat(path).st_uid == 0 and os.lstat(path.parent).st_uid == 0
    except OSError:
        return False


def _reach(item: Item, state: Path) -> None:
    """Refuses an item reached through a link: what lies behind it may be anywhere.

    Inside the state folder (Astra's own, resolved when configured) no step below it may be a link or junction,
    and the real path must stay inside it. A path configured elsewhere is checked as given, every step from the
    top: only the system's own links may lie on the way.
    """
    path = Path(os.path.abspath(item.path))
    home = Path(os.path.abspath(state))
    inside = path != home and path.is_relative_to(home)
    current = home if inside else Path(path.anchor)
    for step in path.relative_to(current).parts:
        current = current / step
        if _is_link(current) and (inside or not _system_link(current)):
            raise OSError(f"{current} is a link; nothing is read or deleted through it")
    if inside and not Path(os.path.realpath(path)).is_relative_to(Path(os.path.realpath(home))):
        raise OSError(f"{item.path} leads outside {home}; nothing is read or deleted there")


def _guard(path: Path, root: Path) -> None:
    """A misconfigured path must never take the home folder, a disk or the installation with it."""
    target = path.resolve()
    for protected in (Path.home().resolve(), root.resolve(), Path(target.anchor)):
        if target == protected or protected.is_relative_to(target):
            raise OSError(f"{path} holds {protected}; it was not deleted")


def _raise(error: OSError) -> None:
    raise error


def _walk(path: Path, links: list[Path] | None = None, *, strict: bool = False
          ) -> Iterator[tuple[Path, list[str], list[str]]]:
    """``os.walk`` that never enters or lists a link: on Windows it walks into a directory junction as into any
    folder (Python 3.11 does not call a junction a link). Each link met is left out and added to ``links``.
    ``strict``: a folder that cannot be read is an error, not passed over."""
    for current, folders, names in os.walk(path, onerror=_raise if strict else None):
        here = Path(current)
        met = [name for name in folders + names if _is_link(here / name)]
        if links is not None:
            links.extend(here / name for name in met)
        folders[:] = sorted(name for name in folders if name not in met)
        names[:] = sorted(name for name in names if name not in met)
        yield here, folders, names


def _files(path: Path, apart: tuple[str, ...] = (), links: list[Path] | None = None) -> Iterator[Path]:
    if _is_link(path) or not path.exists():
        return
    if path.is_file():
        yield path
        return
    for current, folders, names in _walk(path, links):
        top = current == path
        folders[:] = [name for name in folders if name not in _SKIP_NAMES and not (top and name in apart)]
        for name in names:
            if name in _SKIP_NAMES or name.endswith(".lock") or name.endswith(_SQLITE_SIDES):
                continue
            yield current / name


def _linked(links: list[Path], done: str) -> str:
    more = f" (and {len(links) - 1} more)" if len(links) > 1 else ""
    return f"{links[0]} is a link{more}; nothing was {done} through it"


def _credential(path: Path) -> bool:
    name = path.name.lower()
    return (name == ".env" or name.startswith(".env.") or name in _CREDENTIAL_NAMES
            or path.suffix.lower() in _CREDENTIAL_SUFFIXES)


def _size(path: Path, apart: tuple[str, ...] = ()) -> int:
    total = 0
    for entry in _files(path, apart):
        try:
            total += entry.stat().st_size + sum(Path(f"{entry}{side}").stat().st_size
                                                for side in _SQLITE_SIDES if Path(f"{entry}{side}").exists())
        except OSError:
            pass
    return total


def _refusal(item: Item, state: Path) -> str:
    try:
        _reach(item, state)
    except OSError as exc:
        return str(exc)
    return ""


def plan() -> dict:
    """What export and wipe would take, for the person to read before choosing."""
    from agent.runtime.paths import state_dir

    listed = []
    for item in items():
        # Behind a link nothing is measured, exported or deleted; the wipe reports it.
        refused = _refusal(item, state_dir())
        listed.append({"key": item.key, "label": item.label, "path": str(item.path),
                       "present": not refused and item.path.exists(), "bytes": 0 if refused else _size(item.path, item.apart),
                       "export": item.export, "browser": item.browser, "personal": item.personal,
                       **({"refused": refused} if refused else {})})
    return {"items": listed, "not_exported": list(NOT_EXPORTED), "kept": _kept(personal_files=False),
            "confirmation": CONFIRMATION}


def _kept(*, personal_files: bool) -> list[str]:
    return list(KEPT) if personal_files else [*KEPT, PERSONAL_KEPT]


def _copy_sqlite(source: Path, target: Path) -> None:
    with closing(sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10)) as src, \
            closing(sqlite3.connect(target)) as dst:
        src.backup(dst)


def export(out: Path, *, now: float | None = None) -> dict:
    from agent.runtime.paths import state_dir

    # The folder as it really is; the name itself is not resolved, so a link there is replaced, never written through.
    out = out.expanduser()
    out = Path(os.path.realpath(out.parent)) / out.name
    if out.suffix != ".zip":
        raise ValueError("导出文件要以 .zip 结尾。")
    if not out.parent.is_dir():
        raise ValueError(f"找不到文件夹：{out.parent}")
    when = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    # Written to a new file of its own (created exclusively, readable by this user alone) beside the target,
    # then moved over the target's name in one step.
    handle, partial = tempfile.mkstemp(prefix=f".{out.name}.", suffix=".partial", dir=out.parent)
    exported, skipped = [], []
    try:
        with tempfile.TemporaryDirectory(prefix="astra-export-") as scratch, os.fdopen(handle, "w+b") as file, \
                zipfile.ZipFile(file, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for item in items():
                if not item.export:
                    continue
                if refused := _refusal(item, state_dir()):
                    skipped.append({"key": item.key, "label": item.label, "error": refused})
                    continue
                if not item.path.exists():
                    continue
                count = size = 0
                links: list[Path] = []
                for entry in _files(item.path, item.apart, links):
                    if _credential(entry):
                        continue
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
                if links:
                    skipped.append({"key": item.key, "label": item.label, "error": _linked(links, "read")})
            archive.writestr(f"{ARCHIVE_ROOT}/manifest.json", json.dumps({
                "format": "astra-export", "version": 1, "created_at": when.isoformat(timespec="seconds"),
                "items": exported, "not_included": list(NOT_EXPORTED), "skipped": skipped,
            }, ensure_ascii=False, indent=2))
        os.replace(partial, out)
    except BaseException:
        Path(partial).unlink(missing_ok=True)
        raise
    return {"path": str(out), "bytes": out.stat().st_size, "items": exported, "skipped": skipped}


def _others_running(install, *, desktop: bool) -> list[dict]:
    from agent.launcher.locking import active_instances

    # The desktop closes its own sessions and background host first; only its own lease may remain.
    return [item for item in active_instances(install) if not (desktop and item.get("role") == "desktop")]


def _remove(path: Path, tracked: set[Path], *, spare_keys: bool = False) -> tuple[int, list[Path]]:
    """Delete ``path`` (with SQLite's side files), keeping tracked files, and key files when asked. Links inside,
    junctions included, are neither followed nor removed. Returns how many files went and the links left."""
    if _is_link(path):
        return 0, [path]
    if path.is_file():
        if path.resolve() in tracked:
            return 0, []
        path.unlink()
        for side in _SQLITE_SIDES:
            Path(f"{path}{side}").unlink(missing_ok=True)
        return 1, []
    if not path.is_dir():
        return 0, []
    removed, links, folders = 0, [], []
    for current, _folders, names in _walk(path, links, strict=True):
        folders.append(current)
        for name in names:
            entry = current / name
            if entry.resolve() not in tracked and not (spare_keys and _credential(entry)):
                entry.unlink()
                removed += 1
    # Deepest first; a folder that still holds something (a kept file, a link) stays.
    for folder in reversed(folders):
        if not any(folder.iterdir()):
            folder.rmdir()
    return removed, links


def wipe(confirmation: str, *, browser_data: bool = False, personal_files: bool = False, desktop: bool = False,
         root: Path | None = None) -> dict:
    from agent.launcher.installation import discover
    from agent.launcher.locking import exclusive
    from agent.runtime.paths import state_dir

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
        # A folder inside another item's folder goes first, so it is counted (or fails) as itself.
        for item in sorted(items(), key=lambda item: -len(item.path.parts)):
            if (item.browser and not browser_data) or (item.personal and not personal_files):
                continue
            try:
                _reach(item, state_dir())
                _guard(item.path, install.root)
                count, links = _remove(item.path, _tracked(install.root, item.path), spare_keys=item.spare_keys)
            except OSError as exc:
                failed.append({"key": item.key, "label": item.label, "error": str(exc)})
                continue
            if links:
                failed.append({"key": item.key, "label": item.label, "error": _linked(links, "deleted")})
            if count:
                removed.append({"key": item.key, "label": item.label, "files": count})
    return {"removed": removed, "failed": failed, "kept": _kept(personal_files=personal_files)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="astra data", description=(__doc__ or "").split("\n\n")[0])
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("plan", help="list what export and wipe would take")
    out = commands.add_parser("export", help="save the data as one zip")
    out.add_argument("--out", type=Path, required=True)
    erase = commands.add_parser("wipe", help="delete the data; needs --confirm 清除")
    erase.add_argument("--confirm", default="")
    erase.add_argument("--browser-data", action="store_true", help="also delete Astra's browser profiles")
    erase.add_argument("--personal-files", action="store_true", help="also delete the files the personal thread wrote")
    erase.add_argument("--desktop", action="store_true", help=argparse.SUPPRESS)
    erase.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.action == "plan":
            value = plan()
        elif args.action == "export":
            value = export(args.out)
        else:
            value = wipe(args.confirm, browser_data=args.browser_data, personal_files=args.personal_files,
                         desktop=args.desktop, root=args.root)
    except (ValueError, OSError, LauncherError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
    print(json.dumps(value))
    return 1 if value.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
