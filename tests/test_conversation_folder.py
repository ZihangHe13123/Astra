"""Conversation folder behavior through session storage and the backend protocol."""

import json
import os
import threading
import time
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import mkdtemp

import pytest

from test_backend_task_protocol import _start_question_protocol_backend, _stop_protocol_backend
from test_backend_workspace import _ScriptedToolsHandler
from test_session_lifecycle_protocol import send

from agent.runtime.session_recall import SessionRecall
from agent.runtime.session_store import SessionStore
from agent.ui import queries


class _RecordingModelHandler(_ScriptedToolsHandler):
    def do_POST(self):
        if not self.path.endswith("/chat/completions"):
            self.send_error(404)
            return
        request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.requests.append(request)
        chunk = {"id": "chatcmpl-folder", "object": "chat.completion.chunk",
                 "created": int(time.time()), "model": "Qwen3.6-35B-A3B"}
        self._send_sse([
            {**chunk, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "done"}, "finish_reason": None}]},
            {**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ])


class _ProjectBackends:
    def __init__(self, tmp_path, model):
        self.root = tmp_path
        self.model = model
        self.a = tmp_path / "A"
        self.b = tmp_path / "B"
        self.sessions = tmp_path / "sessions"
        self.sessions.mkdir()
        for folder, marker in ((self.a, "marker-a-6c1e"), (self.b, "marker-b-93d2")):
            folder.mkdir()
            (folder / "AGENTS.md").write_text(marker, encoding="utf-8")

    @contextmanager
    def start(self, folder, name, **overrides):
        root = Path(mkdtemp(prefix="backend-", dir=self.root))
        protocol = _start_question_protocol_backend(root, self.model.server_port, name, workdir=folder, env_overrides={
            "ASTRA_TRUSTED_PROJECTS": os.pathsep.join((str(self.a), str(self.b))),
            "SANDBOX_WORKDIR": str(folder),
            "AGENT_SESSION_DIR": str(self.sessions),
            **overrides,
        })
        try:
            yield protocol
        finally:
            _stop_protocol_backend(protocol[0])

    def seed(self, name):
        with self.start(self.a, name) as backend:
            _message(backend, "First message in A")

    def header(self, name):
        return json.loads((self.sessions / f"{name}.header.json").read_text(encoding="utf-8"))


@pytest.fixture
def projects(tmp_path):
    with ThreadingHTTPServer(("127.0.0.1", 0), _RecordingModelHandler) as server:
        server.requests = []
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield _ProjectBackends(tmp_path, server)
        finally:
            server.shutdown()
            thread.join(timeout=5)


def _message(backend, text):
    proc, _, _, wait_for = backend
    send(proc, {"type": "message", "text": text})
    wait_for(lambda event: event.get("type") == "done", timeout=30)


def _session_command(backend, command):
    proc, _, _, wait_for = backend
    send(proc, {"type": "command", "cmd": command})
    result = wait_for(lambda event: event.get("type") == "tool_result" and event.get("name") == "session", timeout=30)
    wait_for(lambda event: event.get("type") == "done", timeout=30)
    return result


def _assert_startup_refused(projects, **overrides):
    with projects.start(projects.b, "conv-a", **overrides) as (proc, events, _, _):
        # The refusal can already have exited when the event reader queues it.
        event = events.get(timeout=30)
        while event.get("type") not in {"model_info", "error"}:
            event = events.get(timeout=30)
        assert event.get("type") == "error" and event.get("code") == "workspace_mismatch", event
        assert os.path.samefile(event["workspace"], projects.a)
        assert proc.wait(timeout=10) == 1


def test_backend_records_the_conversation_workspace(projects):
    projects.seed("conv-a")
    assert projects.header("conv-a")["workspace"] == os.path.realpath(projects.a)


def test_cross_folder_switch_refuses_without_writing_the_target(projects):
    projects.seed("conv-a")
    paths = [projects.sessions / name for name in ("conv-a.header.json", "conv-a.jsonl")]
    before = [path.read_bytes() for path in paths]
    with projects.start(projects.b, "conv-b") as backend:
        _message(backend, "First message in B")
        result = _session_command(backend, "/session conv-a")
        assert result.get("code") == "workspace_mismatch", result
        assert os.path.samefile(result["workspace"], projects.a)
        assert "--here" in result["error"]
        assert "  * conv-b (" in _session_command(backend, "/session")["output"]
    assert [path.read_bytes() for path in paths] == before


def test_switch_here_replaces_guidance_for_models_without_system_updates(projects):
    projects.seed("conv-a")
    with projects.start(projects.b, "conv-b") as backend:
        _message(backend, "First message in B")
        refusal = _session_command(backend, "/session conv-a")
        result = _session_command(backend, "/session conv-a --here")
        new_folder = os.path.realpath(projects.b)
        notice = (f"Moved session 'conv-a' from {os.path.realpath(projects.a)} to {new_folder}. "
                  f"Project instructions for {new_folder} apply from the next turn.")
        assert notice in result["output"].splitlines(), result
        assert refusal.get("code") == "workspace_mismatch", refusal
        _message(backend, "Continue the conversation in B")
        messages = projects.model.requests[-1]["messages"]
    assert projects.header("conv-a")["workspace"] == new_folder
    assert messages[0]["role"] == "system"
    assert "marker-b-93d2" in messages[0]["content"]
    assert "marker-a-6c1e" not in messages[0]["content"]
    assert all(message["role"] != "system" for message in messages[1:])


def test_cross_folder_startup_refuses_and_leaves_no_lease(projects):
    projects.seed("conv-a")
    _assert_startup_refused(projects)
    with projects.start(projects.a, "conv-a") as (_, _, _, wait_for):
        wait_for(lambda event: event.get("type") == "model_info", timeout=30)


def test_startup_move_from_the_recorded_folder_binds_to_the_new_folder(projects):
    projects.seed("conv-a")
    with projects.start(projects.b, "conv-a", ASTRA_CONVERSATION_MOVE_FROM=str(projects.a)) as backend:
        _, _, _, wait_for = backend
        info = wait_for(lambda event: event.get("type") == "session_info", timeout=30)
        assert info.get("workspace") == os.path.realpath(projects.b), info
        assert info.get("folder_notice")
        _message(backend, "Continue after moving from A")
    assert projects.header("conv-a")["workspace"] == os.path.realpath(projects.b)


def test_startup_move_from_a_stale_folder_is_refused(projects):
    projects.seed("conv-a")
    elsewhere = projects.root / "C"
    elsewhere.mkdir()
    _assert_startup_refused(projects, ASTRA_CONVERSATION_MOVE_FROM=str(elsewhere))


def test_personal_thread_follows_its_owner_to_the_new_folder(projects):
    projects.seed("personal")
    with projects.start(projects.b, "personal", ASTRA_PERSONAL_THREAD="1") as backend:
        _, _, _, wait_for = backend
        wait_for(lambda event: event.get("type") == "model_info", timeout=30)
        _message(backend, "Continue the personal thread in B")
    assert projects.header("personal")["workspace"] == os.path.realpath(projects.b)


def test_session_list_limits_to_the_current_folder_unless_all_is_requested(projects):
    projects.seed("conv-a")
    with projects.start(projects.b, "conv-b") as backend:
        _message(backend, "First message in B")
        output = _session_command(backend, "/session")["output"]
        assert "conv-b" in output and "conv-a" not in output, output
        assert output.endswith("1 more in other folders: /session --all")

        output = _session_command(backend, "/session --all")["output"]
        for name, folder in (("conv-a", projects.a), ("conv-b", projects.b)):
            line = next(line for line in output.splitlines() if f" {name} (" in line)
            assert os.path.samefile(os.path.expanduser(line.split(" — ", 1)[1]), folder)


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
