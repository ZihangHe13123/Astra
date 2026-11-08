import os
import sqlite3
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.runtime.maintenance import RuntimeMaintenance, format_maintenance_report
from agent.runtime import maintenance as maintenance_module

pytestmark = pytest.mark.usefixtures("without_astra_home")


def _age(path: Path, days: int) -> None:
    timestamp = time.time() - days * 86_400
    os.utime(path, (timestamp, timestamp))


@pytest.mark.parametrize("platform_name", ["nt", "posix"])
def test_file_signature_keeps_platform_specific_identity_timestamp(monkeypatch, platform_name):
    monkeypatch.setattr(maintenance_module, "os", SimpleNamespace(name=platform_name))
    fields = dict(st_dev=1, st_ino=2, st_mode=0o100600, st_size=3, st_mtime_ns=40)
    path_stat = SimpleNamespace(**fields, st_ctime_ns=50, st_birthtime_ns=50)
    fd_stat = SimpleNamespace(**fields, st_ctime_ns=60, st_birthtime_ns=50)

    if platform_name == "nt":
        assert maintenance_module._file_signature(path_stat) == maintenance_module._file_signature(fd_stat)
        replacement = SimpleNamespace(**fields, st_ctime_ns=60, st_birthtime_ns=70)
        assert maintenance_module._file_signature(path_stat) != maintenance_module._file_signature(replacement)
    else:
        assert maintenance_module._file_signature(path_stat) != maintenance_module._file_signature(fd_stat)

    # Python 3.11 Windows exposes creation time only through ctime.
    legacy_stat = SimpleNamespace(**fields, st_ctime_ns=50)
    assert maintenance_module._file_signature(legacy_stat) == maintenance_module._file_signature(path_stat)


def test_windows_content_fingerprint_retains_handle_change_time_check(tmp_path, monkeypatch):
    path = tmp_path / "candidate"
    path.write_bytes(b"old")
    fields = dict(st_dev=1, st_ino=2, st_mode=0o100600, st_size=3, st_mtime_ns=40, st_birthtime_ns=50)
    path_stat = SimpleNamespace(**fields, st_ctime_ns=50)
    handle_stats = iter([SimpleNamespace(**fields, st_ctime_ns=60), SimpleNamespace(**fields, st_ctime_ns=70)])
    monkeypatch.setattr(maintenance_module, "os", SimpleNamespace(name="nt", fstat=lambda _fd: next(handle_stats)))

    # The path and handle initially disagree on ctime, but two handle reads
    # must still agree. Same-size rewrites with restored mtime remain detectable.
    with pytest.raises(OSError, match="changed during"):
        maintenance_module._content_fingerprint(path, path_stat)


def test_maintenance_preview_is_bounded_and_read_only(tmp_path):
    astra = tmp_path / ".astra"
    artifacts = astra / "tool-results"
    artifacts.mkdir(parents=True)
    expired = artifacts / "old.txt"
    recent = artifacts / "recent.txt"
    expired.write_text("old", encoding="utf-8")
    recent.write_text("new", encoding="utf-8")
    _age(expired, 40)
    outside = tmp_path / "user.txt"
    outside.write_text("keep", encoding="utf-8")
    _age(outside, 40)

    maintenance = RuntimeMaintenance(tmp_path, artifact_dirs=[artifacts, tmp_path])
    report = format_maintenance_report(maintenance, artifact_days=30)

    assert "Expired generated artifacts: 1" in report
    assert expired.exists()
    assert recent.exists()
    assert outside.exists()


def test_maintenance_apply_removes_only_old_owned_artifacts_and_temp(tmp_path):
    astra = tmp_path / ".astra"
    artifacts = astra / "tool-results"
    artifacts.mkdir(parents=True)
    expired = artifacts / "old.txt"
    recent = artifacts / "recent.txt"
    stale_temp = astra / "abandoned.tmp"
    expired.write_text("old", encoding="utf-8")
    recent.write_text("new", encoding="utf-8")
    stale_temp.write_text("tmp", encoding="utf-8")
    _age(expired, 40)
    _age(stale_temp, 2)

    maintenance = RuntimeMaintenance(tmp_path, artifact_dirs=[artifacts])
    result = maintenance.apply(artifact_days=30)

    assert set(result["removed"]) == {str(expired.resolve()), str(stale_temp.resolve())}
    assert not expired.exists()
    assert not stale_temp.exists()
    assert recent.exists()


def test_maintenance_checkpoints_only_owned_sqlite_databases(tmp_path):
    astra = tmp_path / ".astra"
    astra.mkdir()
    database = astra / "tasks.db"
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("CREATE TABLE demo(value TEXT)")
        db.execute("INSERT INTO demo VALUES ('ok')")
    outside = tmp_path / "outside.db"
    with sqlite3.connect(outside) as db:
        db.execute("CREATE TABLE keep(value TEXT)")

    maintenance = RuntimeMaintenance(tmp_path, database_paths=[database, outside])
    snapshots = maintenance.database_snapshot()
    report = format_maintenance_report(maintenance, action="checkpoint")

    assert [item["path"] for item in snapshots] == [str(database.resolve())]
    assert "SQLite PASSIVE checkpoints: 1" in report
    assert "VACUUM was not run" in report


@pytest.mark.parametrize("directory", ["sessions", "memory", "skills", "tasks", "checkpoints"])
def test_durable_directories_are_excluded_even_when_configured_as_artifacts(tmp_path, directory):
    durable = tmp_path / ".astra" / directory
    durable.mkdir(parents=True)
    path = durable / "important.tmp"
    path.write_text("keep")
    _age(path, 40)
    maintenance = RuntimeMaintenance(tmp_path, artifact_dirs=[durable, tmp_path / ".astra"])

    assert maintenance.plan() == []
    assert maintenance.apply()["removed"] == []
    assert path.read_text() == "keep"


def test_nested_temp_cleanup_requires_explicit_generated_directory(tmp_path):
    astra = tmp_path / ".astra"
    generated = astra / "tool-results"
    unknown = astra / "custom-data"
    for directory in (generated, unknown):
        directory.mkdir(parents=True)
        path = directory / "old.tmp"
        path.write_text("keep if not generated")
        _age(path, 2)
    maintenance = RuntimeMaintenance(tmp_path, artifact_dirs=[generated])

    assert [item.path for item in maintenance.plan()] == [generated / "old.tmp"]


@pytest.mark.parametrize("mutation", ["replace", "touch", "rewrite", "symlink", "missing"])
def test_apply_rechecks_candidate_identity_and_time(tmp_path, monkeypatch, mutation):
    astra = tmp_path / ".astra"
    astra.mkdir()
    path = astra / "old.tmp"
    path.write_text("old")
    _age(path, 3)
    maintenance = RuntimeMaintenance(tmp_path)
    candidates = maintenance.plan()
    before = path.stat()
    if mutation == "replace":
        replacement = astra / "replacement"
        replacement.write_text("new")
        os.utime(replacement, ns=(before.st_atime_ns, before.st_mtime_ns))
        replacement.replace(path)
    elif mutation == "touch":
        path.touch()
    elif mutation == "rewrite":
        path.write_text("new")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    else:
        path.unlink()
        if mutation == "symlink":
            target = tmp_path / "outside.txt"
            target.write_text("keep")
            path.symlink_to(target)
    monkeypatch.setattr(maintenance, "plan", lambda **_kwargs: candidates)

    result = maintenance.apply()

    assert result["removed"] == []
    assert result["released_bytes"] == 0
    assert len(result["failed"]) == 1
    if mutation != "missing":
        assert path.exists()


def test_symlinked_astra_root_does_not_own_external_files(tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    path = external / "old.tmp"
    path.write_text("keep")
    _age(path, 3)
    (tmp_path / ".astra").symlink_to(external, target_is_directory=True)

    assert RuntimeMaintenance(tmp_path).plan() == []


@pytest.mark.parametrize("mutation", ["rewrite", "hash_failure", "unchanged"])
def test_windows_content_fingerprint_rechecks_rewrites_and_read_failures(
    tmp_path, monkeypatch, mutation
):
    monkeypatch.setattr(maintenance_module, "_requires_content_fingerprint", lambda: True)
    astra = tmp_path / ".astra"
    astra.mkdir()
    path = astra / "old.tmp"
    path.write_text("old")
    _age(path, 3)
    maintenance = RuntimeMaintenance(tmp_path)
    candidates = maintenance.plan()
    before = path.stat()
    if mutation == "rewrite":
        path.write_text("new")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        # Windows reports creation time in st_ctime, so a rewrite leaves the
        # compared metadata equal. Simulate that equality on POSIX too.
        candidates = [replace(item, changed_ns=path.stat().st_ctime_ns) for item in candidates]
    elif mutation == "hash_failure":
        def unreadable(*_args, **_kwargs):
            raise OSError("cannot verify candidate content")
        monkeypatch.setattr(maintenance_module, "_content_fingerprint", unreadable)
    monkeypatch.setattr(maintenance, "plan", lambda **_kwargs: candidates)

    result = maintenance.apply()

    if mutation == "unchanged":
        assert result["removed"] == [str(path)]
        assert not path.exists()
    else:
        assert result["removed"] == []
        assert result["released_bytes"] == 0
        assert len(result["failed"]) == 1
        assert path.exists()


def test_windows_unreadable_content_is_not_a_cleanup_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance_module, "_requires_content_fingerprint", lambda: True)
    astra = tmp_path / ".astra"
    astra.mkdir()
    path = astra / "old.tmp"
    path.write_text("old")
    _age(path, 3)

    def unreadable(*_args, **_kwargs):
        raise OSError("cannot verify candidate content")

    monkeypatch.setattr(maintenance_module, "_content_fingerprint", unreadable)

    assert RuntimeMaintenance(tmp_path).plan() == []
    assert path.exists()


def test_content_fingerprint_rejects_changes_during_streaming_read(tmp_path, monkeypatch):
    path = tmp_path / "candidate"
    path.write_bytes(b"old")
    before = path.stat()
    real_digest = maintenance_module.hashlib.file_digest

    def rewrite_after_read(stream, algorithm):
        digest = real_digest(stream, algorithm)
        path.write_bytes(b"replacement has a different size")
        return digest

    monkeypatch.setattr(maintenance_module.hashlib, "file_digest", rewrite_after_read)

    with pytest.raises(OSError, match="changed during"):
        maintenance_module._content_fingerprint(path, before)


def test_windows_content_read_rechecks_symlink_before_delete(tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance_module, "_requires_content_fingerprint", lambda: True)
    astra = tmp_path / ".astra"
    astra.mkdir()
    path = astra / "old.tmp"
    path.write_text("old")
    _age(path, 3)
    outside = tmp_path / "keep"
    outside.write_text("keep")
    maintenance = RuntimeMaintenance(tmp_path)
    candidates = maintenance.plan()
    fingerprint = maintenance_module._content_fingerprint

    def replace_after_read(*args):
        digest = fingerprint(*args)
        path.unlink()
        path.symlink_to(outside)
        return digest

    monkeypatch.setattr(maintenance_module, "_content_fingerprint", replace_after_read)
    monkeypatch.setattr(maintenance, "plan", lambda **_kwargs: candidates)

    result = maintenance.apply()

    assert result["removed"] == []
    assert len(result["failed"]) == 1
    assert path.is_symlink()
    assert outside.read_text() == "keep"
