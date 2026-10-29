from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import threading

from agent.runtime.canvas import list_versions, record_version


def test_concurrent_versions_preserve_both_contents(tmp_path, monkeypatch):
    root = tmp_path / "race.canvas"
    root.mkdir()
    (root / "canvas.json").write_text(json.dumps({"type": "doc", "title": "fixture"}))
    content = root / "doc.md"
    content.write_text("VERSION_A")
    a_read, resume_a = threading.Event(), threading.Event()
    mkdir_barrier = threading.Barrier(2)
    original_read, original_mkdir = Path.read_bytes, Path.mkdir

    def read(path):
        data = original_read(path)
        if path == content:
            if data == b"VERSION_A":
                a_read.set()
                assert resume_a.wait(5)
            else:
                resume_a.set()
        return data

    def mkdir(path, *args, **kwargs):
        if path == root / ".versions" / "0001":
            mkdir_barrier.wait(5)
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(Path, "mkdir", mkdir)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(record_version, root, "writer-A")
        assert a_read.wait(5)
        content.write_text("VERSION_B")
        second = pool.submit(record_version, root, "writer-B")
        ids = [first.result(10), second.result(10)]
    assert len(set(ids)) == 2
    versions = list_versions(root)
    assert len(versions) == 2
    contents = []
    for version in versions:
        data = original_read(root / ".versions" / version["id"] / "doc.md")
        assert hashlib.sha256(data).hexdigest() == version["sha256"]
        contents.append(data)
    assert sorted(contents) == [b"VERSION_A", b"VERSION_B"]
