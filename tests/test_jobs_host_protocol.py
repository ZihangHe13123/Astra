"""The desktop's background host runs a scheduled job, pushes it to WeChat and the desktop once, and keeps it in
the personal thread it holds."""

import io
import json
import threading
import time
from http.server import ThreadingHTTPServer

from test_backend_task_protocol import _start_question_protocol_backend, _stop_protocol_backend
from test_session_lifecycle_protocol import send
from test_weixin_backend_protocol import ModelHandler
from weixin_fixture import OWNER, FakeIlink

from agent.runtime.jobs.store import JobStore


class RecordingHandler(ModelHandler):
    conversations: list[list[dict]] = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}"
        payload = json.loads(body)
        if "messages" in payload:
            type(self).conversations.append(payload["messages"])
        self.rfile = io.BytesIO(body)
        super().do_POST()


def test_host_pushes_a_reminder_to_wechat_and_the_desktop_and_keeps_it_in_the_chat(tmp_path):
    RecordingHandler.conversations = []
    model = ThreadingHTTPServer(("127.0.0.1", 0), RecordingHandler)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    ilink = FakeIlink()
    home = tmp_path / "home"
    channels = tmp_path / "channels.json"
    channels.write_text(json.dumps({"weixin": {"keep_awake": False}}), encoding="utf-8")
    workdir = tmp_path / "work"
    workdir.mkdir()
    proc, _, seen, wait_for = _start_question_protocol_backend(
        tmp_path, model.server_port, "jobs_host_session", workdir=workdir,
        env_overrides={"ASTRA_HOME": str(home), "ASTRA_CHANNEL_CONFIG": str(channels),
                       "ASTRA_WEIXIN_BASE_URL": ilink.base, "ASTRA_WEIXIN_CDN_URL": ilink.cdn,
                       "ASTRA_CONTEXT_INDEX_EMBEDDING": "off", "ASTRA_CHANNEL_HOST": "1",
                       "ASTRA_JOBS_TICK_SECONDS": "0.3", "ASTRA_JOBS_PUSH_SECONDS": "0.3",
                       "ASTRA_PERSONAL_THREAD": "1"},
    )
    try:
        wait_for(lambda e: e.get("type") == "weixin_status", timeout=30)
        send(proc, {"type": "command", "cmd": "/weixin login"})
        wait_for(lambda e: e.get("type") == "weixin_status" and e.get("state") == "online", timeout=30)
        # A conversation first, so the reminder joins an existing chat.
        ilink.inbound.put(ilink.text_message("你好", context_token="ctx-owner"))
        deadline = time.monotonic() + 30
        while not ilink.replies_to(OWNER) and time.monotonic() < deadline:
            time.sleep(0.05)

        store = JobStore(home)
        job = store.add(name="喝水", prompt="该喝水了", original_request="提醒我喝水", kind="reminder",
                        workdir=workdir, next_at=time.time(), targets=["desktop", "weixin"])
        notice = wait_for(lambda e: e.get("type") == "job_notification", timeout=30)
        assert notice["title"] == "提醒：喝水" and notice["body"] == "该喝水了" and notice["job_id"] == job["id"]
        deadline = time.monotonic() + 30
        while len(ilink.replies_to(OWNER)) < 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        # One message, not chat bubbles: each message spends what WeChat lets the bot send unasked.
        assert ilink.replies_to(OWNER)[1:] == ["【提醒：喝水】\n该喝水了"]
        time.sleep(1.5)
        sent = len(ilink.replies_to(OWNER))
        # The personal thread (this backend's own conversation) has it too, and the window was told.
        recorded = wait_for(lambda e: e.get("type") == "job_result", timeout=30)
        assert (recorded["title"], recorded["text"], recorded["run_id"]) == ("提醒：喝水", "该喝水了", notice["run_id"])
        assert [(push["target"], push["state"]) for push in store.pushes(notice["run_id"])] == [
            ("desktop", "sent"), ("thread", "sent"), ("weixin", "sent")]

        # The person replies to the reminder: the model sees it in that chat's conversation.
        before = len(RecordingHandler.conversations)
        ilink.inbound.put(ilink.text_message("刚才提醒我什么？", context_token="ctx-owner"))
        deadline = time.monotonic() + 30
        while len(ilink.replies_to(OWNER)) == sent and time.monotonic() < deadline:
            time.sleep(0.05)
        asked = RecordingHandler.conversations[before]
        texts = [str(message.get("content")) for message in asked if message.get("role") == "assistant"]
        assert any("【提醒：喝水】" in text and "该喝水了" in text for text in texts)
        # Each chat turn is a task run from WeChat, for the activity page.
        from agent.runtime.task_store import TaskStore
        # The reply goes out before its run is recorded as finished, so wait for the record.
        deadline = time.monotonic() + 30
        while True:
            runs = TaskStore(tmp_path / "tasks.db").list_tasks(limit=10)
            chat = [run for run in runs if run.get("source") == "weixin"]
            if len(chat) == 2 and all(run["status"] != "running" for run in chat) or time.monotonic() > deadline:
                break
            time.sleep(0.05)
        assert {run["input_text"] for run in chat} == {"你好", "刚才提醒我什么？"}
        assert all(run["status"] == "completed" and run["session_id"].startswith("channel_weixin_private_") for run in chat)
        # Pushed once each, however many ticks passed.
        assert sum("该喝水了" in reply for reply in ilink.replies_to(OWNER)) == 1
        assert len([e for e in seen if e.get("type") == "job_notification"]) == 1
        assert len([e for e in seen if e.get("type") == "job_result"]) == 1

        # Asked in the thread itself, the model sees the reminder there as its own words.
        before = len(RecordingHandler.conversations)
        send(proc, {"type": "message", "text": "刚才那条是什么？"})
        deadline = time.monotonic() + 30
        while len(RecordingHandler.conversations) == before and time.monotonic() < deadline:
            time.sleep(0.05)
        asked = RecordingHandler.conversations[before]
        assert {"role": "assistant", "content": "【提醒：喝水】\n该喝水了"}.items() <= next(
            message for message in asked if "提醒：喝水" in str(message.get("content"))).items()
        assert not any(str(message.get("content")).startswith("你好") for message in asked), "the WeChat chat stays apart"
    finally:
        _stop_protocol_backend(proc)
        model.shutdown()
        model.server_close()
        ilink.close()
