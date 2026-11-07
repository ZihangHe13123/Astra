"""`astra data`: the person's own data as one zip, and wiping it. Every case runs the real command in a
temporary Astra home and a stand-in installation, never against a real one."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from contextlib import closing
from pathlib import Path

import pytest

from agent.launcher.installation import discover
from agent.launcher.locking import RuntimeLease

ROOT = Path(__file__).resolve().parents[1]

# What the person owns; export and wipe take these.
OWNED = {
    "memory/USER.md": "称呼：小林\n", "memory/MEMORY.md": "用中文回答\n",
    "sessions/session_1.json": '{"messages": []}', "sessions/session_1.appshot-media/a.png": "png",
    "sessions/bar/session_2.json": "{}",
    "jobs/preferences.json": '{"quiet_hours": null}', "jobs/runs/r1/session.json": "{}",
    "skills/learned/lab-notes/SKILL.md": "# Lab notes\n", "skills-learning/journal.json": "{}",
}
# What the personal thread wrote in its own folder, its work like a project's: exported, and wiped only when
# chosen too. Keys a conversation left there are never exported.
PERSONAL = {
    "personal/notes/plan.md": "周末计划\n",
    "personal/.env": "ASTRA_MAIL_PASSWORD=personal-secret\n", "personal/notes/.env.local": "TOKEN=personal-token\n",
    "personal/keys/server.pem": "-----BEGIN PRIVATE KEY-----\n",
}
# Copies made from the person's conversations: wiped, not exported.
DERIVED = {
    "gui/history-index/v1.sqlite3": "x", "gui/attachments/a.png": "png", "channels/conversations.json": "{}",
    # From a paired phone: a photo it sent, speech made for it, and what it did.
    "personal/uploads/2026-11-07/101500-abc123-照片.jpg": "jpg",
    "gui/phone-audio/0123456789abcdef0123456789abcdef.wav": "RIFF",
    "gui/phone-audit.jsonl": '{"device": "d1", "method": "upload", "name": "照片.jpg"}\n',
    "personal/.astra/checkpoints/c1/manifest.json": "{}",
}
# Never exported and never wiped: credentials, logins, settings and other apps' state.
KEPT = {
    ".env": "ASTRA_MAIL_PASSWORD=secret\n", "connections/openai.json": '{"api_key": "sk-test"}',
    "auth/codex.json": '{"token": "t"}', "channels/weixin/account.json": '{"token": "t"}',
    "settings.json": "{}", "gui/preferences.json": "{}", "gui/phone-devices.json": "{}", "gui/phone-push-key.json": "{}",
    "skills/creative/bundled/SKILL.md": "# bundled\n", "activity-history.sqlite3": "x",
    "launcher/marker": "x",
}
BROWSER = {"browser-profiles/default/Cookies": "cookie"}
DATABASES = ("memory.db", "sessions.db", "jobs/jobs.sqlite3", "learning.db", "tasks.db", "events.db", "approvals.db",
             "peers.db", "context-vectors-abc.db", "context-index-feedback.db", "browser.db")


def seed(home: Path) -> sqlite3.Connection:
    for name, text in {**OWNED, **PERSONAL, **DERIVED, **KEPT, **BROWSER}.items():
        (home / name).parent.mkdir(parents=True, exist_ok=True)
        (home / name).write_text(text, newline="\n")
    (home / "sessions" / ".writers").mkdir()
    (home / "sessions" / ".writers" / "abc.lock").write_text("")
    (home / "jobs" / "active").write_text("")
    for name in DATABASES:
        # Closed here, not whenever garbage collection gets to it: closing removes the WAL side files.
        with closing(sqlite3.connect(home / name)) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE notes (text TEXT)")
            db.execute("INSERT INTO notes VALUES (?)", (f"committed {name}",))
            db.commit()
    # A running backend's last write still sits in the WAL; an export must include it.
    live = sqlite3.connect(home / "memory.db")
    live.execute("PRAGMA wal_autocheckpoint=0")
    live.execute("INSERT INTO notes VALUES ('only in the wal')")
    live.commit()
    return live


def install_root(tmp_path: Path) -> Path:
    root = tmp_path / "install"
    (root / "agent").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "agent-lab-local"\nversion = "0"\n')
    return root


def environment(home: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith(("AGENT_", "ASTRA_"))}
    return {**env, "ASTRA_HOME": str(home), "AGENT_SESSION_DIR": str(home / "sessions")}


def data(env: dict, *args: str) -> tuple[int, dict]:
    done = subprocess.run([sys.executable, "-m", "agent.cli.data_commands", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=120)
    return done.returncode, json.loads(done.stdout.strip().splitlines()[-1])


def present(home: Path) -> set[str]:
    return {path.relative_to(home).as_posix() for path in home.rglob("*") if path.is_file()}


def test_export_holds_the_persons_data_consistently_and_no_credentials(tmp_path):
    home = tmp_path / "home"
    live = seed(home)
    out = tmp_path / "out" / "astra.zip"
    out.parent.mkdir()
    code, result = data(environment(home), "export", "--out", str(out))
    live.close()
    assert code == 0 and result["path"] == str(out.resolve())
    assert not list(out.parent.glob("*.partial"))
    if os.name == "posix":
        assert out.stat().st_mode & 0o777 == 0o600
    with zipfile.ZipFile(out) as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("astra-export/manifest.json"))
        archive.extract("astra-export/memory/memory.db", tmp_path / "x")
        assert archive.read("astra-export/core-user/USER.md").decode() == "称呼：小林\n"
    expected = {"astra-export/core-user/USER.md", "astra-export/core-memory/MEMORY.md", "astra-export/memory/memory.db",
                "astra-export/recall/sessions.db", "astra-export/learning/learning.db",
                "astra-export/sessions/session_1.json", "astra-export/sessions/session_1.appshot-media/a.png",
                "astra-export/sessions/bar/session_2.json", "astra-export/jobs/jobs.sqlite3",
                "astra-export/jobs/preferences.json", "astra-export/jobs/runs/r1/session.json",
                "astra-export/skills/lab-notes/SKILL.md", "astra-export/skills-journal/journal.json",
                "astra-export/personal/notes/plan.md", "astra-export/manifest.json"}
    assert names == expected
    with sqlite3.connect(tmp_path / "x" / "astra-export" / "memory" / "memory.db") as copy:
        assert {row[0] for row in copy.execute("SELECT text FROM notes")} == {"committed memory.db", "only in the wal"}
    assert [item["key"] for item in manifest["items"]][:3] == ["core-user", "core-memory", "memory"]
    assert "微信登录" in manifest["not_included"]
    assert not any(secret in out.read_bytes() for secret in (b"sk-test", b"ASTRA_MAIL_PASSWORD", b"cookie",
                                                             b"personal-secret", b"personal-token", b"PRIVATE KEY"))


@pytest.mark.skipif(os.name == "nt", reason="making links needs extra rights on Windows")
def test_an_export_never_writes_through_a_link_at_its_name(tmp_path):
    home = tmp_path / "home"
    seed(home).close()
    outside = {name: tmp_path / name for name in ("notes.txt", "old.zip")}
    for path in outside.values():
        path.write_text("someone else's file")
    folder = tmp_path / "out"
    folder.mkdir()
    # A link where the half-written archive used to go, and one at the chosen name itself.
    (folder / "astra.zip.partial").symlink_to(outside["notes.txt"])
    (folder / "linked.zip").symlink_to(outside["old.zip"])
    for name in ("astra.zip", "linked.zip"):
        code, result = data(environment(home), "export", "--out", str(folder / name))
        assert code == 0, result
        assert not (folder / name).is_symlink() and zipfile.is_zipfile(folder / name)
    assert all(path.read_text() == "someone else's file" for path in outside.values())
    assert sorted(path.name for path in folder.iterdir()) == ["astra.zip", "astra.zip.partial", "linked.zip"]


def test_plan_names_what_each_choice_takes(tmp_path):
    home = tmp_path / "home"
    seed(home).close()
    code, result = data(environment(home), "plan")
    by_key = {item["key"]: item for item in result["items"]}
    assert code == 0 and result["confirmation"] == "清除"
    assert by_key["memory"]["export"] and by_key["memory"]["present"] and by_key["memory"]["bytes"] > 0
    assert not by_key["tasks"]["export"] and by_key["browser-profiles"]["browser"]
    assert by_key["vectors:context-vectors-abc.db"]["present"]
    # The phone's uploads and the edit snapshots sit inside the personal folder but are counted once, as themselves.
    own = sum(len(text.encode()) for text in PERSONAL.values())
    assert by_key["personal"]["export"] and by_key["personal"]["personal"] and by_key["personal"]["bytes"] == own
    assert not by_key["phone-uploads"]["personal"] and "个人对话写的文件（选了一起清除才删）" in result["kept"]
    assert by_key["phone-uploads"]["present"] and not by_key["phone-uploads"]["export"]
    assert by_key["phone-audit"]["present"] and by_key["phone-audio"]["present"]
    listed = {Path(item["path"]).resolve() for item in result["items"]}
    assert not {(home / name).resolve() for name in KEPT} & listed


def test_wipe_needs_the_word_and_takes_the_data_and_its_copies_but_not_logins(tmp_path):
    home, root = tmp_path / "home", install_root(tmp_path)
    seed(home).close()
    env = environment(home)
    before = present(home)
    code, result = data(env, "wipe", "--root", str(root), "--confirm", "清楚")
    assert code == 1 and "清除" in result["error"] and present(home) == before
    code, result = data(env, "wipe", "--root", str(root), "--confirm", "清除")
    assert code == 0 and not result["failed"]
    left = present(home)
    assert set(KEPT) | set(BROWSER) | set(PERSONAL) | {"browser.db"} <= left
    assert not (set(OWNED) | set(DERIVED)) & left
    assert "个人对话写的文件（选了一起清除才删）" in result["kept"]
    assert not {name for name in left if name.endswith(("-wal", "-shm"))} - {"browser.db-wal", "browser.db-shm"}
    assert {item["key"] for item in result["removed"]} >= {"memory", "sessions", "recall", "jobs", "skills", "tasks",
                                                           "vectors:context-vectors-abc.db", "history-cache"}
    code, result = data(env, "wipe", "--root", str(root), "--confirm", "清除", "--browser-data")
    assert code == 0 and not set(BROWSER) & present(home) and not (home / "browser.db").exists()
    assert set(KEPT) | set(PERSONAL) <= present(home)
    code, result = data(env, "wipe", "--root", str(root), "--confirm", "清除", "--personal-files")
    assert code == 0 and not set(PERSONAL) & present(home) and not (home / "personal").exists()
    assert [item["key"] for item in result["removed"]] == ["personal"] and not any("个人对话" in k for k in result["kept"])
    assert set(KEPT) <= present(home)


def test_wipe_waits_for_every_other_astra_to_close(tmp_path):
    home, root = tmp_path / "home", install_root(tmp_path)
    seed(home).close()
    env = environment(home)
    before = present(home)
    with RuntimeLease(discover(root), "backend"):
        code, result = data(env, "wipe", "--root", str(root), "--confirm", "清除", "--desktop")
    assert code == 1 and "backend" in result["error"] and present(home) == before
    # The desktop's own lease may stay: it closed its sessions and background host before asking.
    with RuntimeLease(discover(root), "desktop"):
        assert data(env, "wipe", "--root", str(root), "--confirm", "清除")[0] == 1
        code, _ = data(env, "wipe", "--root", str(root), "--confirm", "清除", "--desktop")
    assert code == 0 and not (home / "memory.db").exists()


def test_a_source_checkout_keeps_what_git_tracks(tmp_path):
    root = install_root(tmp_path)
    home = root / ".astra"
    seed(home).close()
    git = ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q"], check=True)
    (home / "skills" / "learned" / "shared-skill").mkdir(parents=True)
    (home / "skills" / "learned" / "shared-skill" / "SKILL.md").write_text("# shared\n")
    subprocess.run([*git, "add", ".astra/skills/learned/shared-skill/SKILL.md"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "skill"], check=True)
    code, result = data(environment(home), "wipe", "--root", str(root), "--confirm", "清除")
    assert code == 0 and not result["failed"]
    assert (home / "skills" / "learned" / "shared-skill" / "SKILL.md").exists()
    assert not (home / "skills" / "learned" / "lab-notes").exists()


@pytest.mark.skipif(os.name == "nt", reason="making links needs extra rights on Windows")
def test_a_link_in_the_astra_folder_is_never_followed_by_export_or_wipe(tmp_path):
    home, root = tmp_path / "home", install_root(tmp_path)
    seed(home).close()
    env = environment(home)
    # The personal folder and the pasted images lead elsewhere: a parent folder and an item that are links.
    elsewhere, pictures = tmp_path / "elsewhere", tmp_path / "pictures"
    shutil.move(home / "personal", elsewhere)
    (home / "personal").symlink_to(elsewhere, target_is_directory=True)
    shutil.move(home / "gui" / "attachments", pictures)
    (home / "gui" / "attachments").symlink_to(pictures, target_is_directory=True)
    kept = present(elsewhere) | {f"pictures/{name}" for name in present(pictures)}

    code, result = data(env, "plan")
    by_key = {item["key"]: item for item in result["items"]}
    assert "is a link" in by_key["phone-uploads"]["refused"] and not by_key["phone-uploads"]["present"]
    out = tmp_path / "astra.zip"
    code, result = data(env, "export", "--out", str(out))
    assert code == 0 and [item["key"] for item in result["skipped"]] == ["personal"]
    with zipfile.ZipFile(out) as archive:
        assert not [name for name in archive.namelist() if name.startswith("astra-export/personal/")]

    code, result = data(env, "wipe", "--root", str(root), "--confirm", "清除")
    failed = {item["key"]: item["error"] for item in result["failed"]}
    assert code == 1 and {"phone-uploads", "personal-copies", "attachments"} <= set(failed)
    assert all("is a link" in error for error in failed.values())
    assert present(elsewhere) | {f"pictures/{name}" for name in present(pictures)} == kept
    assert (home / "personal").is_symlink() and (home / "gui" / "attachments").is_symlink()
    # Everything not behind a link went as usual.
    assert not (home / "memory.db").exists() and not (home / "sessions").exists()


@pytest.mark.skipif(os.name == "nt", reason="making links needs extra rights on Windows")
def test_a_path_configured_elsewhere_is_checked_from_the_top(tmp_path):
    home, root = tmp_path / "home", install_root(tmp_path)
    seed(home).close()
    real = tmp_path / "real"
    real.mkdir()
    shutil.move(home / "sessions", real / "sessions")
    (tmp_path / "linkdir").symlink_to(real, target_is_directory=True)
    before = present(real)
    # The conversations are configured outside the state folder, below a folder that is a link.
    env = {**environment(home), "AGENT_SESSION_DIR": str(tmp_path / "linkdir" / "sessions")}
    code, result = data(env, "wipe", "--root", str(root), "--confirm", "清除")
    failed = {item["key"]: item["error"] for item in result["failed"]}
    assert code == 1 and f"{tmp_path / 'linkdir'} is a link" in failed["sessions"]
    assert present(real) == before
    # The system's own links on the way do not count: macOS reaches its temporary folders through /var.
    through_var = Path(str(real).replace("/private/var/", "/var/", 1))
    if through_var != real and Path("/var").is_symlink():
        env["AGENT_SESSION_DIR"] = str(through_var / "sessions")
        code, result = data(env, "wipe", "--root", str(root), "--confirm", "清除")
        assert code == 0 and not result["failed"] and not (real / "sessions").exists()


def test_a_misconfigured_path_never_takes_the_installation_with_it(tmp_path):
    home, root = tmp_path / "home", install_root(tmp_path)
    seed(home).close()
    env = {**environment(home), "AGENT_SESSION_DIR": str(root)}
    code, result = data(env, "wipe", "--root", str(root), "--confirm", "清除")
    assert code == 1 and [item["key"] for item in result["failed"]] == ["sessions"]
    assert (root / "pyproject.toml").exists() and not (home / "memory.db").exists()


def test_the_astra_command_runs_the_same_data_commands(tmp_path):
    home = tmp_path / "home"
    seed(home).close()
    done = subprocess.run([sys.executable, str(ROOT / "astra.py"), "data", "plan"], cwd=tmp_path, env=environment(home),
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1])["confirmation"] == "清除"
    helped = subprocess.run([sys.executable, str(ROOT / "astra.py"), "data", "--help"], cwd=tmp_path,
                            env=environment(home), capture_output=True, text=True, timeout=120)
    assert helped.returncode == 0 and "wipe" in helped.stdout
