"""A local stand-in for Tencent's iLink Bot API and WeChat's media CDN."""

from __future__ import annotations

import json
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

BOT_ID = "bot-0001@im.bot"
BOT_TOKEN = "fixture-bot-token-not-real-0000"
OWNER = "owner-0001@im.wechat"
STRANGER = "stranger-0002@im.wechat"
LOGIN_LINK = "https://liteapp.weixin.qq.com/q/fixture?qrcode=code-1"


class FakeIlink:
    """Scripted iLink: login states, a queue of inbound messages, recorded replies."""

    def __init__(self) -> None:
        self.login_states = ["wait", "scaned", "confirmed"]
        self.inbound: queue.Queue[dict] = queue.Queue()
        self.update_answers: queue.Queue[dict] = queue.Queue()
        self.send_answers: queue.Queue[dict] = queue.Queue()
        self.sent: list[dict] = []
        self.polls: list[dict] = []
        self.headers: list[dict] = []
        self.media: dict[str, bytes] = {}
        self.media_requests: list[str] = []
        self._cursor = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _json(self, payload: dict) -> None:
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                url = urlparse(self.path)
                query = parse_qs(url.query)
                if url.path == "/ilink/bot/get_bot_qrcode":
                    self._json({"qrcode": "code-1", "qrcode_img_content": LOGIN_LINK})
                elif url.path == "/ilink/bot/get_qrcode_status":
                    state = fake.login_states.pop(0) if len(fake.login_states) > 1 else fake.login_states[0]
                    answer: dict = {"status": state}
                    if state == "confirmed":
                        answer.update(ilink_bot_id=BOT_ID, bot_token=BOT_TOKEN, baseurl=fake.base,
                                      ilink_user_id=OWNER)
                    self._json(answer)
                elif url.path == "/c2c/download":
                    key = query.get("encrypted_query_param", [""])[0]
                    fake.media_requests.append(key)
                    data = fake.media.get(key)
                    if data is None:
                        self.send_error(404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                else:
                    self.send_error(404)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                fake.headers.append(dict(self.headers))
                if self.path == "/ilink/bot/getupdates":
                    fake.polls.append(payload)
                    try:
                        self._json(fake.update_answers.get_nowait())
                        return
                    except queue.Empty:
                        pass
                    messages = []
                    deadline = time.monotonic() + 0.3
                    while time.monotonic() < deadline and not messages:
                        try:
                            messages.append(fake.inbound.get(timeout=0.05))
                        except queue.Empty:
                            continue
                    if messages:
                        fake._cursor += 1
                    self._json({"ret": 0, "msgs": messages, "get_updates_buf": f"cursor-{fake._cursor}",
                                "longpolling_timeout_ms": 500})
                elif self.path == "/ilink/bot/sendmessage":
                    fake.sent.append(payload["msg"])
                    try:
                        self._json(fake.send_answers.get_nowait())
                    except queue.Empty:
                        self._json({"ret": 0})
                else:
                    self.send_error(404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.cdn = f"{self.base}/c2c"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def text_message(self, text: str, *, sender: str = OWNER, message_id: str = "", context_token: str = "ctx-1") -> dict:
        return {"from_user_id": sender, "to_user_id": BOT_ID, "message_id": message_id or f"m-{time.monotonic_ns()}",
                "context_token": context_token, "item_list": [{"type": 1, "text_item": {"text": text}}]}

    def replies_to(self, user: str) -> list[str]:
        return ["".join(item["text_item"]["text"] for item in message["item_list"])
                for message in self.sent if message["to_user_id"] == user]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
