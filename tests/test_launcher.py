"""Behavior-level contracts for the cross-platform installation entry layer."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from agent.launcher import cli, dependencies, installation, services, setup, update
from agent.launcher.common import LauncherError, git, read_json, write_json
from agent.launcher.installation import discover, runtime_environment
from agent.launcher.locking import RuntimeLease, active_instances, exclusive
from agent.launcher.transaction import Transaction
from agent.runtime.paths import env_file, sessions_dir, state_path

ROOT = Path(__file__).resolve().parents[1]


def git_command(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def write_source(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "agent").mkdir(exist_ok=True)
    (root / "agent/__init__.py").write_text("")
    (root / "astra.py").write_text("# source bootstrap fixture\n")
    for name in ("astra.sh", "astra.bat"):
        shutil.copy2(ROOT / name, root / name)
    (root / "ui-tui").mkdir(exist_ok=True)
    (root / "pyproject.toml").write_text('[project]\nname="agent-lab-local"\nversion="0.2.0"\n', encoding="utf-8")
    (root / "uv.lock").write_text("fixture lock\n", encoding="utf-8")
    (root / "ui-tui/package.json").write_text("{}\n")
    (root / "ui-tui/package-lock.json").write_text("{}\n")
    (root / ".gitignore").write_text(".astra/\n.sessions/\n.env\n.venv/\nui-tui/node_modules/\nui-tui/dist/\n")


@pytest.fixture
def source(tmp_path, monkeypatch):
    # No launcher fixture may query or pause the developer's installed services.
    monkeypatch.setattr(services, "adapter_for", lambda install: services.NoServices())
    for name in ("ASTRA_HOME", "AGENT_SESSION_DIR", "ASTRA_ENV_FILE", "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    seed = tmp_path / "seed"
    write_source(seed)
    git_command(seed, "init", "-b", "main")
    git_command(seed, "config", "user.name", "Launcher Test")
    git_command(seed, "config", "user.email", "launcher@example.invalid")
    git_command(seed, "add", ".")
    git_command(seed, "commit", "-m", "initial")
    remote = tmp_path / "remote.git"
    git_command(tmp_path, "clone", "--bare", str(seed), str(remote))
    checkout = tmp_path / "Astra 中文 with spaces"
    git_command(tmp_path, "clone", str(remote), str(checkout))
    git_command(seed, "remote", "add", "origin", str(remote))
    inst = discover(checkout)
    for name in (".venv", "ui-tui/node_modules", "ui-tui/dist"):
        folder = checkout / name
        folder.mkdir(parents=True)
        (folder / "previous.txt").write_text(name)
    for folder, name in ((inst.data, "settings.json"), (inst.sessions, "conversation.json")):
        folder.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text("user data")
    (checkout / ".env").write_text("private configuration")
    monkeypatch.setattr(update, "legacy_processes", lambda install: [])
    monkeypatch.setattr(dependencies, "uv_command", lambda *args: "uv")
    monkeypatch.setattr(dependencies, "check_node", lambda root: "22.0.0")
    monkeypatch.setattr(dependencies, "python_health", lambda install: None)
    monkeypatch.setattr(dependencies, "validate", lambda install: None)

    def sync(install, extras, **plan):
        for name in (".venv", "ui-tui/node_modules", "ui-tui/dist"):
            (install.root / name / "previous.txt").write_text("new runtime")

    def record(install, extras):
        write_json(install.control / "installation.json", {"schema": 1, "root": str(install.root), "extras": extras})
        write_json(install.control / "environment.json", {"fingerprint": dependencies.fingerprint(install)})

    monkeypatch.setattr(dependencies, "synchronize", sync)
    monkeypatch.setattr(dependencies, "record_environment", record)
    return inst, seed


def advance(seed: Path, filename: str = "change.txt") -> str:
    (seed / filename).write_text("new version\n", encoding="utf-8")
    git_command(seed, "add", filename)
    git_command(seed, "commit", "-m", "next version")
    git_command(seed, "push", "origin", "main")
    return git_command(seed, "rev-parse", "HEAD")


def assert_private_data(inst) -> None:
    assert (inst.data / "settings.json").read_text() == "user data"
    assert (inst.sessions / "conversation.json").read_text() == "user data"
    assert (inst.root / ".env").read_text() == "private configuration"


def assert_old_environments(inst) -> None:
    for name in (".venv", "ui-tui/node_modules", "ui-tui/dist"):
        assert (inst.root / name / "previous.txt").read_text() == name


def test_management_entry_has_no_site_packages_or_credentials(tmp_path):
    env = {key: value for key, value in os.environ.items() if "API_KEY" not in key and key != "PYTHONPATH"}
    result = subprocess.run([sys.executable, "-I", "-S", str(ROOT / "astra.py"), "version", "--json"],
                            cwd=tmp_path, env=env, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["root"] == str(ROOT)
    assert value["kind"] == "source"
    assert not (tmp_path / ".astra").exists()


def test_discovery_does_not_follow_callers_git_repo(source, tmp_path, monkeypatch):
    inst, _ = source
    elsewhere = tmp_path / "user project"
    elsewhere.mkdir()
    git_command(elsewhere, "init")
    monkeypatch.chdir(elsewhere)
    assert discover(inst.root).root == inst.root
    env = runtime_environment(inst, elsewhere)
    assert env["ASTRA_WORKSPACE"] == str(elsewhere)
    assert env["AGENT_PROJECT_ROOT"] == str(inst.root)
    assert env["AGENT_SESSION_DIR"] == str(inst.sessions)


def test_custom_data_and_specific_overrides_preserve_source_configuration(source, tmp_path, monkeypatch):
    inst, _ = source
    data = tmp_path / "private profile"
    sessions = tmp_path / "separate sessions"
    monkeypatch.setenv("ASTRA_HOME", str(data))
    monkeypatch.setenv("AGENT_SESSION_DIR", str(sessions))
    selected = discover(inst.root)
    assert selected.data == data
    assert selected.sessions == sessions
    assert selected.control == inst.control
    assert state_path("memory.db") == data / "memory.db"
    assert sessions_dir() == sessions
    assert env_file(inst.root) == inst.root / ".env"
    env = runtime_environment(selected, tmp_path)
    assert env["ASTRA_ENV_FILE"] == str(inst.root / ".env")


def test_package_installation_uses_user_state_and_refuses_git(tmp_path, monkeypatch):
    # Simulate site-packages containing the launcher, with no source pyproject.
    package = tmp_path / "site-packages/agent/launcher"
    package.mkdir(parents=True)
    monkeypatch.setattr(installation, "__file__", str(package / "installation.py"))
    monkeypatch.setattr(installation, "user_home", lambda: tmp_path / "user data")
    monkeypatch.delenv("ASTRA_HOME", raising=False)
    inst = discover()
    assert inst.kind == "installed"
    assert inst.data == tmp_path / "user data"
    monkeypatch.setattr(update, "git", lambda *args: pytest.fail("must not call git for package-owned installation"))
    with pytest.raises(LauncherError, match="original.*wheel or package manager"):
        update.update_source(inst)


def test_moved_installation_can_be_diagnosed_and_recreated_without_discarding_state(source, tmp_path):
    inst, _ = source
    write_json(inst.control / "installation.json", {"schema": 1, "root": str(tmp_path / "old location"),
                                                     "extras": ["mcp", "tracing"]})
    assert inst.describe()["relocated"] is True
    assert dependencies.is_ready(inst) is False
    with pytest.raises(LauncherError, match="moved or was copied"):
        dependencies.check_environment_ownership(inst)
    for path in (inst.root / ".venv", inst.ui / "node_modules"):
        shutil.rmtree(path)
    dependencies.check_environment_ownership(inst)
    assert inst.metadata["extras"] == ["mcp", "tracing"]
    assert_private_data(inst)


def test_update_check_is_usable_while_runtime_is_active(source):
    inst, seed = source
    before = git(inst.root, "rev-parse", "HEAD")
    target = advance(seed)
    with RuntimeLease(inst, "test interface"):
        result = update.update_source(inst, check=True)
        assert result["outcome"] == "available"
        assert result["target"] == target
        assert git(inst.root, "rev-parse", "HEAD") == before
        assert_old_environments(inst)
        assert_private_data(inst)
        assert not (inst.control / "pending.json").exists()


def test_active_runtime_blocks_apply_and_another_update(source):
    inst, seed = source
    advance(seed)
    with RuntimeLease(inst, "test interface"):
        with pytest.raises(LauncherError, match="Close these Astra"):
            update.update_source(inst)
    with exclusive(inst):
        with pytest.raises(LauncherError, match="Another Astra"):
            update.update_source(inst, check=True)
        with pytest.raises(LauncherError, match="Another Astra"):
            RuntimeLease(inst, "racing startup")
    assert active_instances(inst) == []


def test_direct_backend_registers_before_loading_optional_runtime(source, monkeypatch):
    from agent.launcher import locking
    inst, _ = source
    callbacks = []
    monkeypatch.setattr(locking, "discover", lambda: inst)
    monkeypatch.setattr(locking.atexit, "register", callbacks.append)
    # Protecting a backend should not need API credentials or heavy imports.
    before = dict(os.environ)
    try:
        locking.protect_backend()
        assert active_instances(inst)[0]["role"] == "backend"
        assert os.environ["ASTRA_HOME"] == str(inst.data)
        assert os.environ["AGENT_PROJECT_ROOT"] == str(inst.root)
    finally:
        for callback in callbacks:
            callback()
        os.environ.clear()
        os.environ.update(before)
    assert active_instances(inst) == []


def test_update_applies_exact_commit_and_preserves_user_data(source):
    inst, seed = source
    target = advance(seed)
    result = update.update_source(inst)
    assert result["outcome"] == "applied"
    assert result["activation"] == "next launch"
    assert git(inst.root, "rev-parse", "HEAD") == target
    assert (inst.root / ".venv/previous.txt").read_text() == "new runtime"
    assert_private_data(inst)
    assert not (inst.control / "pending.json").exists()
    assert read_json(inst.control / "receipts/latest.json")["outcome"] == "applied"


@pytest.mark.parametrize("failing_step", ["synchronize", "validate", "record_environment"])
def test_failures_restore_code_and_environments(source, monkeypatch, failing_step):
    inst, seed = source
    before = git(inst.root, "rev-parse", "HEAD")
    advance(seed)

    def fail(*args, **kwargs):
        (inst.root / ".venv/previous.txt").write_text("partially updated")
        raise LauncherError("injected failure")

    monkeypatch.setattr(dependencies, failing_step, fail)
    with pytest.raises(LauncherError, match="previous installation was restored"):
        update.update_source(inst)
    assert git(inst.root, "rev-parse", "HEAD") == before
    assert_old_environments(inst)
    assert_private_data(inst)
    assert read_json(inst.control / "receipts/latest.json")["outcome"] == "recovered"
    assert not (inst.control / "pending.json").exists()


def test_tracked_local_changes_are_not_stashed_or_overwritten(source):
    inst, seed = source
    advance(seed, "uv.lock")
    changed = inst.root / "uv.lock"
    changed.write_text("my local change\n")
    with pytest.raises(LauncherError, match="Local tracked changes"):
        update.update_source(inst)
    assert changed.read_text() == "my local change\n"
    assert git(inst.root, "stash", "list") == ""
    assert_old_environments(inst)


def test_untracked_collision_is_preserved_after_failed_fast_forward(source):
    inst, seed = source
    before = git(inst.root, "rev-parse", "HEAD")
    advance(seed, "user-file.txt")
    local = inst.root / "user-file.txt"
    local.write_text("my untracked file\n")
    with pytest.raises(LauncherError, match="Choose --keep-local or --overwrite-local"):
        update.update_source(inst)
    assert local.read_text() == "my untracked file\n"
    assert git(inst.root, "rev-parse", "HEAD") == before
    assert_old_environments(inst)


@pytest.mark.parametrize("policy", ["keep", "overwrite"])
def test_explicit_local_policy_retains_a_durable_binary_backup(source, policy):
    inst, seed = source
    advance(seed, "astra.py")
    target = advance(seed, "other.txt")
    local = inst.root / "astra.py"
    content = b"local\x00binary\xff\r\n"
    local.write_bytes(content)
    outcome = update.update_source(inst, local_policy=policy)
    assert outcome["outcome"] == "applied"
    assert outcome["local_policy"] == policy
    if policy == "keep":
        assert local.read_bytes() == content
    else:
        assert local.read_text() == "new version\n"
    assert (inst.root / "other.txt").read_text() == "new version\n"
    assert git(inst.root, "rev-parse", "HEAD") == target
    folder = Path(outcome["local_backup"])
    saved = read_json(folder / "manifest.json")
    assert saved["files"]["astra.py"]["kind"] == "file"
    assert (folder / "files/astra.py").read_bytes() == content
    assert git(inst.root, "stash", "list") == ""
    assert_private_data(inst)


def test_nonoverlapping_local_file_is_kept_without_prompt(source):
    inst, seed = source
    target = advance(seed)
    path = inst.root / "astra.py"
    path.write_text("my local source\n")
    result = update.update_source(inst, choose_local=lambda _: pytest.fail("unrelated files need no decision"))
    assert result["local_policy"] == "keep"
    assert path.read_text() == "my local source\n"
    assert git(inst.root, "rev-parse", "HEAD") == target


@pytest.mark.parametrize("worktree_content", ["unstaged content\n", "# source bootstrap fixture\n"])
def test_keep_preserves_staged_and_unstaged_contents_separately(source, worktree_content):
    inst, seed = source
    advance(seed, "astra.py")
    local = inst.root / "astra.py"
    local.write_text("staged content\n")
    git_command(inst.root, "add", "astra.py")
    local.write_text(worktree_content)
    result = update.update_source(inst, local_policy="keep")
    assert result["outcome"] == "applied"
    assert local.read_text() == worktree_content
    assert git_command(inst.root, "show", ":astra.py") == "staged content"
    assert git(inst.root, "diff", "--cached", "--name-only") == "astra.py"


@pytest.mark.parametrize("staged", [False, True])
def test_keep_preserves_a_local_deletion(source, staged):
    inst, seed = source
    advance(seed, "astra.py")
    (inst.root / "astra.py").unlink()
    if staged:
        git_command(inst.root, "add", "astra.py")
    update.update_source(inst, local_policy="keep")
    assert not (inst.root / "astra.py").exists()
    assert bool(git(inst.root, "diff", "--cached", "--name-only")) is staged


def test_keep_preserves_a_staged_rename(source):
    inst, seed = source
    advance(seed, "astra.py")
    git_command(inst.root, "mv", "astra.py", "my-local.py")
    update.update_source(inst, local_policy="keep")
    assert not (inst.root / "astra.py").exists()
    assert (inst.root / "my-local.py").read_text() == "# source bootstrap fixture\n"
    assert "my-local.py" in git(inst.root, "diff", "--cached", "--name-only")


@pytest.mark.parametrize("policy", ["keep", "overwrite"])
def test_untracked_incoming_collision_requires_and_obeys_a_choice(source, policy):
    inst, seed = source
    advance(seed, "notes.txt")
    (inst.root / "notes.txt").write_text("my untracked notes\n")
    (inst.root / "unrelated.txt").write_text("never touch this\n")
    update.update_source(inst, local_policy=policy)
    expected = "my untracked notes\n" if policy == "keep" else "new version\n"
    assert (inst.root / "notes.txt").read_text() == expected
    assert (inst.root / "unrelated.txt").read_text() == "never touch this\n"


def test_cancel_and_noninteractive_choice_leave_the_checkout_unchanged(source):
    inst, seed = source
    before = git(inst.root, "rev-parse", "HEAD")
    advance(seed, "astra.py")
    (inst.root / "astra.py").write_text("my work\n")
    with pytest.raises(LauncherError, match="Choose --keep-local"):
        update.update_source(inst)
    result = update.update_source(inst, choose_local=lambda _: "cancel")
    assert result["outcome"] == "cancelled"
    assert (inst.root / "astra.py").read_text() == "my work\n"
    assert git(inst.root, "rev-parse", "HEAD") == before
    assert not (inst.control / "pending.json").exists()
    assert not (inst.control / "local-changes").exists()
    assert_old_environments(inst)


@pytest.mark.parametrize("policy", ["keep", "overwrite"])
def test_update_failure_restores_original_index_worktree_and_runtime(source, monkeypatch, policy):
    inst, seed = source
    before = git(inst.root, "rev-parse", "HEAD")
    advance(seed, "astra.py")
    path = inst.root / "astra.py"
    path.write_text("staged work\n")
    git_command(inst.root, "add", "astra.py")
    path.write_text("unfinished work\n")

    def fail(_):
        raise LauncherError("incompatible local override")

    monkeypatch.setattr(dependencies, "validate", fail)
    with pytest.raises(LauncherError, match="previous installation was restored"):
        update.update_source(inst, local_policy=policy)
    assert git(inst.root, "rev-parse", "HEAD") == before
    assert git(inst.root, "show", ":astra.py") == "staged work"
    assert path.read_text() == "unfinished work\n"
    assert_old_environments(inst)
    assert_private_data(inst)
    assert not (inst.control / "pending.json").exists()


@pytest.mark.parametrize("phase", ["prepared", "cleared", "updated", "restored"])
def test_interrupted_local_update_restores_all_original_work(source, phase):
    from agent.launcher.local_changes import LocalChanges
    inst, seed = source
    before = git(inst.root, "rev-parse", "HEAD")
    advance(seed, "astra.py")
    target, _ = update.fetch_target(inst)
    path = inst.root / "astra.py"
    path.write_text("my original local work\n")
    transaction = Transaction(inst, before, target)
    transaction.attach_local(LocalChanges(inst, before, target), "keep")
    transaction.prepare()
    if phase != "prepared":
        transaction.clear_local()
    if phase in {"updated", "restored"}:
        git(inst.root, "merge", "--ff-only", target)
        transaction.save("code_updated")
    if phase == "restored":
        transaction.apply_local()
    assert update.update_source(inst, recover=True)["outcome"] == "recovered"
    assert git(inst.root, "rev-parse", "HEAD") == before
    assert path.read_text() == "my original local work\n"
    assert_old_environments(inst)


def test_concurrent_local_edit_stops_recovery_without_overwriting_it(source, monkeypatch):
    inst, seed = source
    advance(seed, "astra.py")
    path = inst.root / "astra.py"
    path.write_text("original local work\n")

    def edit_and_fail(_):
        path.write_text("new edit made during update\n")
        raise LauncherError("validation stopped")

    monkeypatch.setattr(dependencies, "validate", edit_and_fail)
    with pytest.raises(LauncherError, match="recovery needs attention"):
        update.update_source(inst, local_policy="keep")
    assert path.read_text() == "new edit made during update\n"
    assert (inst.control / "pending.json").exists()


def test_current_version_does_not_discard_local_edits_or_require_idle(source, monkeypatch):
    inst, _ = source
    (inst.root / "astra.py").write_text("customized current version\n")
    monkeypatch.setattr(dependencies, "is_ready", lambda _: True)
    monkeypatch.setattr(dependencies, "node_health", lambda _: None)
    with RuntimeLease(inst, "active interface"):
        result = update.update_source(inst, local_policy="overwrite")
    assert result["outcome"] == "current"
    assert (inst.root / "astra.py").read_text() == "customized current version\n"


def test_external_commit_during_validation_is_not_reported_as_the_update_target(source, monkeypatch):
    inst, seed = source
    advance(seed, "astra.py")
    (inst.root / "astra.py").write_text("local source\n")
    git_command(inst.root, "config", "user.name", "Fixture")
    git_command(inst.root, "config", "user.email", "fixture@example.invalid")
    external = []

    def commit_elsewhere(_):
        git_command(inst.root, "commit", "--allow-empty", "-m", "concurrent commit")
        external.append(git(inst.root, "rev-parse", "HEAD"))

    monkeypatch.setattr(dependencies, "validate", commit_elsewhere)
    with pytest.raises(LauncherError, match="recovery needs attention"):
        update.update_source(inst, local_policy="keep")
    assert git(inst.root, "rev-parse", "HEAD") == external[0]
    assert (inst.root / "astra.py").read_text() == "local source\n"
    assert (inst.control / "pending.json").exists()


@pytest.mark.parametrize(("answer", "policy"), [("", "keep"), ("1", "keep"), ("2", "overwrite"), ("3", "cancel")])
def test_interactive_file_choices(monkeypatch, capsys, answer, policy):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _: answer)
    preview = {"before": "a" * 40, "target": "b" * 40,
               "local_details": {"files": ["a.py"], "conflicts": ["a.py"]}}
    assert cli.choose_local_files(preview) == policy
    assert "Back up local files" in capsys.readouterr().out


@pytest.mark.parametrize("staged", [False, True])
def test_identical_incoming_content_needs_no_choice(source, staged):
    inst, seed = source
    advance(seed, "astra.py")
    (inst.root / "astra.py").write_text("new version\n")
    if staged:
        git_command(inst.root, "add", "astra.py")
    result = update.update_source(inst, choose_local=lambda _: pytest.fail("identical files need no decision"))
    assert result["outcome"] == "applied"
    assert git(inst.root, "status", "--porcelain", "--untracked-files=no") == ""


def test_a_file_edited_during_the_prompt_is_not_overwritten(source):
    inst, seed = source
    before = git(inst.root, "rev-parse", "HEAD")
    advance(seed, "astra.py")
    path = inst.root / "astra.py"
    path.write_text("first local edit\n")

    def choose(_):
        path.write_text("new edit while choosing\n")
        return "overwrite"

    with pytest.raises(LauncherError, match="after the update preview"):
        update.update_source(inst, choose_local=choose)
    assert path.read_text() == "new edit while choosing\n"
    assert git(inst.root, "rev-parse", "HEAD") == before
    assert not (inst.control / "pending.json").exists()


def test_keep_and_rollback_preserve_exact_crlf_bytes(source, monkeypatch):
    inst, seed = source
    before = git(inst.root, "rev-parse", "HEAD")
    advance(seed, "astra.py")
    git_command(inst.root, "config", "core.autocrlf", "true")
    local = inst.root / "astra.py"
    content = b"local windows line\r\nsecond line\r\n"
    local.write_bytes(content)

    def fail(_):
        assert local.read_bytes() == content
        raise LauncherError("rollback CRLF fixture")

    monkeypatch.setattr(dependencies, "validate", fail)
    with pytest.raises(LauncherError, match="previous installation was restored"):
        update.update_source(inst, local_policy="keep")
    assert local.read_bytes() == content
    assert git(inst.root, "rev-parse", "HEAD") == before


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable permissions")
def test_keep_retains_local_executable_mode(source):
    inst, seed = source
    advance(seed, "astra.py")
    path = inst.root / "astra.py"
    path.write_text("local executable\n")
    path.chmod(0o755)
    update.update_source(inst, local_policy="keep")
    assert path.read_text() == "local executable\n"
    assert path.stat().st_mode & 0o777 == 0o755


def test_ignored_collision_is_detected_instead_of_silently_overwritten(source):
    inst, seed = source
    advance(seed, "ignored.txt")
    git_directory = Path(git(inst.root, "rev-parse", "--absolute-git-dir"))
    (git_directory / "info/exclude").write_text("ignored.txt\n")
    path = inst.root / "ignored.txt"
    path.write_text("local ignored content\n")
    with pytest.raises(LauncherError, match="Choose --keep-local"):
        update.update_source(inst)
    update.update_source(inst, local_policy="keep")
    assert path.read_text() == "local ignored content\n"


@pytest.mark.parametrize("policy", ["keep", "overwrite"])
def test_retiring_a_bundled_mode_preserves_its_private_extension_and_sessions(source, policy):
    inst, seed = source
    relative = "agent/retired_mode.py"
    advance(seed, relative)
    update.update_source(inst)
    (inst.root / relative).write_text("private local implementation\n")
    private = {
        inst.data / "local_mode.py": b"# private controller\n",
        inst.data / "persona.local.json": b'{"schema":1,"profiles":[]}\n',
        inst.sessions / "custom/existing.json": b'{"messages":[]}\n',
    }
    for path, content in private.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (seed / relative).unlink()
    git_command(seed, "add", "-u")
    git_command(seed, "commit", "-m", "retire bundled mode")
    git_command(seed, "push", "origin", "main")

    result = update.update_source(inst, local_policy=policy)

    assert result["outcome"] == "applied"
    assert_private_data(inst)
    assert all(path.read_bytes() == content for path, content in private.items())
    if policy == "keep":
        assert (inst.root / relative).read_text() == "private local implementation\n"
    else:
        assert not (inst.root / relative).exists()


def test_upstream_cannot_replace_private_configuration_even_with_overwrite(source):
    inst, seed = source
    (seed / ".env").write_text("incoming configuration\n")
    git_command(seed, "add", "-f", ".env")
    git_command(seed, "commit", "-m", "invalid private-file update")
    git_command(seed, "push", "origin", "main")
    with pytest.raises(LauncherError, match="private/generated state"):
        update.update_source(inst, local_policy="overwrite")
    assert_private_data(inst)
    assert not (inst.control / "pending.json").exists()


def test_new_ignored_collision_after_preview_is_not_overwritten(source):
    inst, seed = source
    advance(seed, "astra.py")
    advance(seed, "ignored.txt")
    git_directory = Path(git(inst.root, "rev-parse", "--absolute-git-dir"))
    (git_directory / "info/exclude").write_text("ignored.txt\n")
    (inst.root / "astra.py").write_text("my local source\n")

    def choose(_):
        (inst.root / "ignored.txt").write_text("created during preview\n")
        return "overwrite"

    with pytest.raises(LauncherError, match="incoming path changed"):
        update.update_source(inst, choose_local=choose)
    assert (inst.root / "ignored.txt").read_text() == "created during preview\n"


def test_process_blocker_also_reports_local_overlap(source, monkeypatch):
    inst, seed = source
    advance(seed, "astra.py")
    (inst.root / "astra.py").write_text("my local source\n")
    monkeypatch.setattr(update, "legacy_processes", lambda _: ["PID 42: Astra browser recorder"])
    checked = update.update_source(inst, check=True)
    assert checked["local_details"]["conflicts"] == ["astra.py"]
    assert "browser recorder" in checked["process_holders"][0]
    with pytest.raises(LauncherError, match="(?s)browser recorder.*Overlapping incoming files.*astra.py"):
        update.update_source(inst, local_policy="keep")


def test_noninteractive_input_does_not_assume_a_local_choice(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    with pytest.raises(LauncherError, match="Rerun in a terminal"):
        cli.choose_local_files({})


def test_intent_to_add_is_not_silently_converted_to_a_staged_empty_file(source):
    inst, seed = source
    advance(seed)
    (inst.root / "draft.txt").write_text("not yet staged\n")
    git_command(inst.root, "add", "-N", "draft.txt")
    with pytest.raises(LauncherError, match="intent-to-add"):
        update.update_source(inst, local_policy="keep")
    assert git(inst.root, "diff", "--cached", "--name-only") == ""
    assert (inst.root / "draft.txt").read_text() == "not yet staged\n"


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_hidden_local_edits_are_not_overwritten(source, flag):
    inst, seed = source
    advance(seed, "astra.py")
    git_command(inst.root, "update-index", flag, "astra.py")
    (inst.root / "astra.py").write_text("hidden local edit\n")
    with pytest.raises(LauncherError, match="Hidden index flags"):
        update.update_source(inst, local_policy="overwrite")
    assert (inst.root / "astra.py").read_text() == "hidden local edit\n"


@pytest.mark.skipif(os.name == "nt", reason="newline filenames are POSIX-only")
def test_keep_uses_literal_nul_delimited_filenames(source):
    inst, seed = source
    name = "local [file]\nname.txt"
    advance(seed, name)
    path = inst.root / name
    path.write_bytes(b"my local bytes\r\n")
    result = update.update_source(inst, local_policy="keep")
    assert path.read_bytes() == b"my local bytes\r\n"
    assert (Path(result["local_backup"]) / "files" / name).read_bytes() == b"my local bytes\r\n"


def test_diverged_checkout_is_not_reset(source):
    inst, seed = source
    advance(seed)
    git_command(inst.root, "config", "user.name", "Test")
    git_command(inst.root, "config", "user.email", "test@example.invalid")
    (inst.root / "mine.txt").write_text("local commit")
    git_command(inst.root, "add", "mine.txt")
    git_command(inst.root, "commit", "-m", "local change")
    local = git(inst.root, "rev-parse", "HEAD")
    with pytest.raises(LauncherError, match="ahead of or diverge"):
        update.update_source(inst)
    assert git(inst.root, "rev-parse", "HEAD") == local
    assert_old_environments(inst)


def test_shared_worktree_environment_cannot_be_modified(source, tmp_path):
    inst, seed = source
    advance(seed)
    shared = tmp_path / "owner environment"
    (inst.root / ".venv").rename(shared)
    try:
        (inst.root / ".venv").symlink_to(shared, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks require Windows Developer Mode")
    with pytest.raises(LauncherError, match="Shared/symlinked"):
        update.update_source(inst)
    assert (shared / "previous.txt").read_text() == ".venv"


def test_interrupted_update_requires_explicit_recovery(source):
    inst, seed = source
    before = git(inst.root, "rev-parse", "HEAD")
    target = advance(seed)
    fetched, _ = update.fetch_target(inst)
    transaction = Transaction(inst, before, fetched)
    transaction.prepare()
    git(inst.root, "merge", "--ff-only", target)
    transaction.save("code_updated")
    (inst.root / ".venv/previous.txt").write_text("partial update")
    with pytest.raises(LauncherError, match="interrupted"):
        RuntimeLease(inst, "new interface")
    result = update.update_source(inst, recover=True)
    assert result["outcome"] == "recovered"
    assert git(inst.root, "rev-parse", "HEAD") == before
    assert_old_environments(inst)
    assert_private_data(inst)


def test_recovery_preserves_edits_made_after_update_started(source):
    inst, seed = source
    before = git(inst.root, "rev-parse", "HEAD")
    advance(seed, "uv.lock")
    target, _ = update.fetch_target(inst)
    transaction = Transaction(inst, before, target)
    transaction.prepare()
    git(inst.root, "merge", "--ff-only", target)
    (inst.root / "uv.lock").write_text("concurrent edit\n")
    with pytest.raises(LauncherError):
        update.update_source(inst, recover=True)
    assert (inst.root / "uv.lock").read_text() == "concurrent edit\n"
    assert (inst.control / "pending.json").exists()


def test_same_commit_with_unverified_environment_is_repaired(source):
    inst, _ = source
    before = git(inst.root, "rev-parse", "HEAD")
    result = update.update_source(inst)
    assert result["outcome"] == "applied"
    assert git(inst.root, "rev-parse", "HEAD") == before
    assert (inst.root / ".venv/previous.txt").read_text() == "new runtime"


def test_verified_code_only_update_does_not_reinstall_dependencies(source, monkeypatch):
    inst, seed = source
    advance(seed)
    monkeypatch.setattr(dependencies, "is_ready", lambda install: True)
    monkeypatch.setattr(dependencies, "node_health", lambda install: None)
    plans = []
    monkeypatch.setattr(dependencies, "synchronize", lambda install, extras, **plan: plans.append(plan))
    assert update.update_source(inst)["outcome"] == "applied"
    assert plans == [{"python": False, "node": False, "build": False}]
    assert_old_environments(inst)


def test_current_but_broken_runtime_is_repaired(source, monkeypatch):
    inst, _ = source
    monkeypatch.setattr(dependencies, "is_ready", lambda install: True)

    def broken(install):
        raise LauncherError("missing dependency")

    monkeypatch.setattr(dependencies, "python_health", broken)
    assert update.update_source(inst)["outcome"] == "applied"
    assert (inst.root / ".venv/previous.txt").read_text() == "new runtime"


def test_source_archive_setup_recovery_does_not_need_git(source, tmp_path):
    inst, _ = source
    # A Git-less source archive does not require deleting read-only Git objects
    # on Windows. Keep fixture metadata outside the simulated installation.
    metadata = inst.root / ".git"
    archived_metadata = tmp_path / "archive-metadata.git"
    assert metadata.resolve().is_relative_to(tmp_path.resolve())
    assert archived_metadata.absolute().is_relative_to(tmp_path.resolve())
    assert not archived_metadata.exists()
    metadata.rename(archived_metadata)
    transaction = Transaction(inst, "", "")
    transaction.prepare()
    (inst.root / ".venv/previous.txt").write_text("partial archive setup")
    assert update.update_source(inst, recover=True)["outcome"] == "recovered"
    assert_old_environments(inst)


def test_recovery_finalizes_a_validated_committed_update(source):
    inst, _ = source
    before = git(inst.root, "rev-parse", "HEAD")
    transaction = Transaction(inst, before, before)
    transaction.prepare()
    (inst.root / ".venv/previous.txt").write_text("validated runtime")
    transaction.save("committed")
    assert update.update_source(inst, recover=True)["outcome"] == "applied"
    assert (inst.root / ".venv/previous.txt").read_text() == "validated runtime"


def test_legacy_relative_env_state_uses_selected_profile(source, tmp_path, monkeypatch):
    from agent.cli.environment import load_project_env
    inst, _ = source
    profile = tmp_path / "profile"
    monkeypatch.setenv("ASTRA_HOME", str(profile))
    monkeypatch.setenv("AGENT_SKILLS_PATH", ".astra/skills")
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "explicit settings.json"))
    load_project_env(inst.root)
    assert os.environ["AGENT_SKILLS_PATH"] == str(profile / "skills")
    assert os.environ["AGENT_SETTINGS_PATH"] == str(tmp_path / "explicit settings.json")


def test_explicit_workdir_in_project_env_takes_precedence_over_invocation_default(source, tmp_path, monkeypatch):
    from agent.cli.environment import load_project_env
    monkeypatch.setattr(os, "environ", os.environ.copy())
    inst, _ = source
    explicit = tmp_path / "configured workspace"
    monkeypatch.delenv("SANDBOX_WORKDIR", raising=False)
    monkeypatch.delenv("ASTRA_WORKSPACE", raising=False)
    # dotenv double quotes interpret Windows sequences such as \a and \t.
    (inst.root / ".env").write_text(f"SANDBOX_WORKDIR='{explicit}'\n", encoding="utf-8")
    for name, value in runtime_environment(inst, tmp_path).items():
        monkeypatch.setenv(name, value)
    load_project_env(inst.root)
    assert os.environ["SANDBOX_WORKDIR"] == str(explicit)


def test_update_network_failure_leaves_installation_intact(source, monkeypatch):
    inst, _ = source
    before = git(inst.root, "rev-parse", "HEAD")

    def offline(install):
        raise LauncherError("network unavailable")

    monkeypatch.setattr(update, "fetch_target", offline)
    with pytest.raises(LauncherError, match="network unavailable"):
        update.update_source(inst)
    assert git(inst.root, "rev-parse", "HEAD") == before
    assert_old_environments(inst)
    assert_private_data(inst)
    assert not (inst.control / "pending.json").exists()


def test_private_uv_bootstrap_never_installs_into_the_global_interpreter(source, monkeypatch):
    inst, _ = source
    # Exercise ensure_uv independently of the fixture's available-uv shortcut.
    calls = []
    local = inst.control / "bootstrap" / ("Scripts/uv.exe" if os.name == "nt" else "bin/uv")

    def uv(install):
        if local.exists():
            return str(local)
        raise LauncherError("missing")

    def run_command(args, **kwargs):
        calls.append(args)
        if "pip" in args:
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text("installed")
        return ""

    monkeypatch.setattr(dependencies, "uv_command", uv)
    monkeypatch.setattr(dependencies, "run", run_command)
    assert dependencies.ensure_uv(inst) == str(local)
    assert calls[0][1:3] == ["-m", "venv"]
    assert Path(calls[1][0]).is_relative_to(inst.control / "bootstrap")
    assert "uv==0.11.27" in calls[1]


def test_git_environment_cannot_redirect_update(source, tmp_path, monkeypatch):
    inst, seed = source
    advance(seed)
    monkeypatch.setenv("GIT_DIR", str(seed / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(seed))
    result = update.update_source(inst)
    assert result["root"] == str(inst.root)


def test_source_command_install_is_idempotent_and_preserves_arguments(source, tmp_path):
    inst, _ = source
    if os.name == "nt":
        pytest.skip("POSIX shim; Windows shim has a native test below")
    (inst.root / "astra.py").write_text("import json,sys;print(json.dumps(sys.argv[1:]));sys.exit(19)")
    bin_dir = tmp_path / "personal bin"
    command = setup.install_command(inst, bin_dir=bin_dir, modify_path=False)
    original = command.read_bytes()
    assert setup.install_command(inst, bin_dir=bin_dir, modify_path=False).read_bytes() == original
    result = subprocess.run([str(command), "update", "two words", "中文"], cwd=tmp_path,
                            capture_output=True, text=True, check=False)
    assert result.returncode == 19
    assert json.loads(result.stdout) == ["update", "two words", "中文"]
    command.write_text("a different application's command")
    with pytest.raises(LauncherError, match="Another installation owns"):
        setup.install_command(inst, bin_dir=bin_dir, modify_path=False)


@pytest.mark.skipif(os.name == "nt", reason="migration of a POSIX legacy shim")
def test_known_legacy_source_shim_is_backed_up_and_migrated(source, tmp_path):
    inst, _ = source
    directory = tmp_path / "legacy bin"
    directory.mkdir()
    command = directory / "astra"
    legacy = setup._legacy_source_shim(inst.root) + "\n"
    command.write_text(legacy)
    setup.install_command(inst, bin_dir=directory, modify_path=False)
    assert setup.SHIM_MARKER in command.read_text()
    backups = list((inst.control / "command-backups").glob("*.sh"))
    assert len(backups) == 1
    assert backups[0].read_text() == legacy


def test_snapshot_updater_runs_without_optional_packages_or_model_keys(source, tmp_path):
    inst, _ = source
    # The command-only path exercises the real isolated zipapp handoff, not a mock.
    result = cli.isolated_maintenance(inst, ["setup", "--command-only", "--no-path", "--bin-dir", str(tmp_path / "bin")])
    assert result == 0
    assert (tmp_path / "bin" / ("astra.cmd" if os.name == "nt" else "astra")).is_file()


@pytest.mark.skipif(os.name != "nt", reason="native Windows CMD acceptance")
@pytest.mark.parametrize("wrapper", ["astra.bat"])
@pytest.mark.parametrize("shell", ["cmd", "powershell"])
@pytest.mark.parametrize("rewrite_launcher", [False, True])
def test_native_windows_argument_cwd_and_exit_forwarding(tmp_path, wrapper, shell, rewrite_launcher):
    root = tmp_path / "Astra 中文 & spaces"
    root.mkdir()
    for name in ("astra.bat",):
        shutil.copy2(ROOT / name, root / name)
    rewrite = (
        'from pathlib import Path;'
        '(Path(__file__).parent/"astra.bat").write_text("@echo off\\nexit /b 99\\n");'
        if rewrite_launcher else ""
    )
    (root / "astra.py").write_text(
        'import os,sys,json;' + rewrite + 'print(json.dumps([sys.argv[1:],os.getcwd()]));sys.exit(23)'
    )
    env = dict(os.environ, PYTHON=str(Path(getattr(sys, "_base_executable", sys.executable)).resolve()), PYTHONUTF8="1")
    line = f'""{root / wrapper}" version "two words" "中文" "literal!bang" "a & b" "100%literal""'
    # CMD uses its own quoting grammar, not the CRT list2cmdline backslash rules.
    command = "cmd.exe /d /s /c " + line
    if shell == "powershell":
        path = str(root / wrapper).replace("'", "''")
        command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                   f"[Console]::OutputEncoding=[Text.UTF8Encoding]::new(); & '{path}' version 'two words' '中文' 'literal!bang' 'a & b' '100%literal'; exit $LASTEXITCODE"]
    result = subprocess.run(command, cwd=tmp_path, env=env,
                            capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 23, result.stdout + result.stderr
    assert json.loads(result.stdout) == [["version", "two words", "中文", "literal!bang", "a & b", "100%literal"], str(tmp_path)]


def test_gui_is_opt_in_and_shared_core_precedes_clients(source):
    install, _ = source
    (install.root / "ui-core").mkdir()
    (install.root / "ui-core/package.json").write_text("{}")
    (install.root / "ui-core/package-lock.json").write_text("{}")
    assert [p.name for p in dependencies.source_ui_packages(install)] == ["ui-core", "ui-tui"]
    assert [p.name for p in dependencies.source_ui_packages(install, gui=True)] == ["ui-core", "ui-tui", "ui-gui"]
    before = dependencies.fingerprint(install)
    (install.root / "ui-gui").mkdir()
    (install.root / "ui-gui/package.json").write_text('{"changed":true}')
    assert dependencies.fingerprint(install) == before
    (install.root / "ui-core/package-lock.json").write_text('{"changed":true}')
    assert dependencies.fingerprint(install) != before


def test_failed_gui_setup_restores_component_choice_and_files(source, monkeypatch):
    install, _ = source
    generated = install.root / "ui-gui/dist"
    generated.mkdir(parents=True)
    (generated / "old.txt").write_text("old")
    def fail(inst, _extras):
        assert dependencies.gui_enabled(inst)
        (generated / "old.txt").write_text("broken")
        raise LauncherError("GUI build failed")
    monkeypatch.setattr(dependencies, "synchronize", fail)
    with pytest.raises(LauncherError, match="restored"):
        setup.setup_source(install, [], gui=True)
    assert not dependencies.gui_enabled(install)
    assert (generated / "old.txt").read_text() == "old"
    assert not (install.control / "gui-environment.json").exists()
    assert_private_data(install)


def test_gui_health_does_not_download_electron(source, monkeypatch):
    install, _ = source
    monkeypatch.setattr(dependencies, "run", lambda *args, **kwargs: "")
    with pytest.raises(LauncherError, match="Electron binary missing"):
        dependencies.gui_health(install)
    assert not (install.root / "ui-gui").exists()


def test_gui_entry_is_separate_from_default_tui(source, monkeypatch):
    from agent.launcher import gui
    install, _ = source
    calls = []
    monkeypatch.setattr(gui, "launch_gui", lambda inst: calls.append("gui") or 0)
    monkeypatch.setattr(cli, "launch", lambda inst, **kwargs: calls.append("tui") or 0)
    assert cli.main(["--gui"], root=install.root) == 0
    assert cli.main([], root=install.root) == 0
    assert calls == ["gui", "tui"]
    assert cli.parser().parse_args(["setup", "--gui"]).setup_gui
    with pytest.raises(SystemExit):
        cli.main(["--gui", "--tui"], root=install.root)


def test_gui_code_updates_rebuild_only_when_enabled(source, monkeypatch):
    install, seed = source
    (seed / "ui-gui").mkdir()
    after = advance(seed, "ui-gui/change.ts")
    git_command(install.root, "fetch", "origin")
    before = git_command(install.root, "rev-parse", "HEAD")
    monkeypatch.setattr(dependencies, "is_ready", lambda inst: True)
    monkeypatch.setattr(dependencies, "gui_ready", lambda inst: True)
    assert update.environment_plan(install, before, after, False) == {"python":False,"node":False,"build":False}
    write_json(install.control / "installation.json", {"schema":1,"root":str(install.root),"gui":True})
    assert update.environment_plan(install, before, after, False) == {"python":False,"node":False,"build":True}
