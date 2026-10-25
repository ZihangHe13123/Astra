"""The desktop's background host runs a scheduled job and pushes it to WeChat and the desktop, once."""

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
                       "ASTRA_JOBS_TICK_SECONDS": "0.3", "ASTRA_JOBS_PUSH_SECONDS": "0.3"},
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
                        workdir=workdir, next_at=time.time())
        notice = wait_for(lambda e: e.get("type") == "job_notification", timeout=30)
        assert notice["title"] == "提醒：喝水" and notice["body"] == "该喝水了" and notice["job_id"] == job["id"]
        deadline = time.monotonic() + 30
        while len(ilink.replies_to(OWNER)) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        # Sent as chat bubbles, like a reply.
        assert ilink.replies_to(OWNER)[1:] == ["【提醒：喝水】", "该喝水了"]
        time.sleep(1.5)
        sent = len(ilink.replies_to(OWNER))
        assert [push["state"] for push in store.pushes(notice["run_id"])] == ["sent", "sent"]

        # The person replies to the reminder: the model sees it in that chat's conversation.
        before = len(RecordingHandler.conversations)
        ilink.inbound.put(ilink.text_message("刚才提醒我什么？", context_token="ctx-owner"))
        deadline = time.monotonic() + 30
        while len(ilink.replies_to(OWNER)) == sent and time.monotonic() < deadline:
            time.sleep(0.05)
        asked = RecordingHandler.conversations[before]
        texts = [str(message.get("content")) for message in asked if message.get("role") == "assistant"]
        assert any("【提醒：喝水】" in text and "该喝水了" in text for text in texts)
        # Pushed once each, however many ticks passed.
        assert sum("该喝水了" in reply for reply in ilink.replies_to(OWNER)) == 1
        assert len([e for e in seen if e.get("type") == "job_notification"]) == 1
    finally:
        _stop_protocol_backend(proc)
        model.shutdown()
        model.server_close()
        ilink.close()
