"""Conversation folder behavior through session storage and the backend protocol."""

import json

from agent.runtime.session_recall import SessionRecall
from agent.runtime.session_store import SessionStore
from agent.ui import queries


def test_listing_infers_legacy_folders_without_writing_sessions(tmp_path, monkeypatch):
    project = tmp_path / "A"
    project.mkdir()
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(queries.sessions, "SESSION_DIR", sessions)
    monkeypatch.setattr(queries.sessions, "SESSION_DEFAULT", sessions / "default.json")

    for name in ("legacy-guidance", "legacy-recall", "legacy-none"):
        store = SessionStore(sessions / f"{name}.json")
        store.save({"messages": [{"role": "user", "content": "saved conversation"}]})
        header = json.loads(store.header_path.read_text(encoding="utf-8"))
        header.pop("workspace", None)
        header.pop("guidance_snapshot", None)
        if name == "legacy-guidance":
            header["guidance_snapshot"] = {"scope": {"workdir": str(project)}}
        store.header_path.write_text(json.dumps(header), encoding="utf-8")

    recall = SessionRecall()
    try:
        recall.init_db()
        recall.get_or_create_session("legacy-recall", workspace_root=str(project))
    finally:
        recall.close()

    def file_stamps():
        stamps = {}
        for path in sessions.rglob("*"):
            if path.is_file():
                stat = path.stat()
                stamps[path] = (stat.st_size, stat.st_mtime_ns)
        return stamps

    before = file_stamps()
    result = {item["name"]: (item["workspace"], item["workspace_source"]) for item in queries.list_sessions()}
    assert result == {
        "legacy-guidance": (str(project), "guidance"),
        "legacy-recall": (str(project), "recall"),
        "legacy-none": (None, ""),
    }
    assert file_stamps() == before


def test_listing_reuses_header_read_until_the_header_changes(tmp_path, monkeypatch):
    from agent.runtime import conversation_folder

    store = SessionStore(tmp_path / "cached.json")
    folder = str(tmp_path / "A")
    new_folder = str(tmp_path / "another-folder")
    store.save({"workspace": folder, "messages": [{"role": "user", "content": "saved conversation"}]})
    conversation_folder.clear_caches()
    reads = []
    read_header = conversation_folder._read_header

    def counted_read(path):
        reads.append(path)
        return read_header(path)

    monkeypatch.setattr(conversation_folder, "_read_header", counted_read)
    assert conversation_folder.listing_folder(store) == (folder, "recorded")
    assert conversation_folder.listing_folder(store) == (folder, "recorded")
    assert reads == [store.header_path]

    header = json.loads(store.header_path.read_text(encoding="utf-8"))
    header["workspace"] = new_folder
    store.header_path.write_text(json.dumps(header), encoding="utf-8")
    assert conversation_folder.listing_folder(store) == (new_folder, "recorded")
    assert reads == [store.header_path, store.header_path]
