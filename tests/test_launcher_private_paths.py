"""Incoming source trees cannot alias private state on Windows or macOS."""

from types import SimpleNamespace

import pytest

from agent.launcher import local_changes, update
from agent.launcher.common import LauncherError
from test_launcher import assert_private_data, git_command, source as source_fixture

source = source_fixture


@pytest.mark.parametrize("name", [
    ".env", ".ENV", ".ASTRA/settings.json", ".astra", ".sessions", ".SESSIONS/chat.json",
    ".venv", ".VENV/Scripts/python.exe", "UI-TUI/DIST/main.js", "ui-core/node_modules",
    "UI-GUI/NODE_MODULES/electron/cli.js",
])
def test_private_names_reserved_case_insensitively(tmp_path, monkeypatch, name):
    install = SimpleNamespace(root=tmp_path)
    monkeypatch.setattr(local_changes, "_tree", lambda _, commit: {} if commit == "before" else {name: ["100644", "oid"]})
    monkeypatch.setattr(local_changes, "_index", lambda _: {})
    with pytest.raises(LauncherError, match="private/generated state"):
        local_changes.LocalChanges(install, "before", "target")


def test_shared_skills_are_still_allowed(tmp_path, monkeypatch):
    install = SimpleNamespace(root=tmp_path)
    name = ".astra/skills/example/SKILL.md"
    monkeypatch.setattr(local_changes, "_tree", lambda _, commit: {} if commit == "before" else {name: ["100644", "oid"]})
    monkeypatch.setattr(local_changes, "_index", lambda _: {})
    monkeypatch.setattr(local_changes, "git_bytes", lambda *args: b"")
    changes = local_changes.LocalChanges(install, "before", "target")
    assert changes.paths == []


@pytest.mark.parametrize("name", [".ENV", ".ASTRA/settings.json"])
def test_overwrite_cannot_bypass_case_alias_protection(source, name):
    inst, seed = source
    incoming = seed / name
    incoming.parent.mkdir(parents=True, exist_ok=True)
    incoming.write_text("must not replace private data", encoding="utf-8")
    git_command(seed, "add", "-f", name)
    git_command(seed, "commit", "-m", "case alias in incoming source")
    git_command(seed, "push", "origin", "main")
    with pytest.raises(LauncherError, match="private/generated state"):
        update.update_source(inst, local_policy="overwrite")
    assert_private_data(inst)
    assert not (inst.control / "pending.json").exists()
