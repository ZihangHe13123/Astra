"""WeChat over iLink: login, who is answered, replies, expiry, one poller, images, staying awake."""

import asyncio
import base64
import os
import subprocess
import sys

import pytest
from weixin_fixture import BOT_TOKEN, LOGIN_LINK, OWNER, STRANGER, FakeIlink

from agent.channels.base import ChannelMessage
from agent.channels.config import WeixinConfig
from agent.channels.router import AgentChannelRouter
from agent.channels.weixin import CHAT_STYLE, IlinkClient, WeixinAdapter, WeixinStore, chat_bubbles, qr_login
from agent.runtime.context import AgentContext


class FakeAwake:
    def __init__(self) -> None:
        self.active = False
        self.starts = 0

    def start(self) -> bool:
        self.starts += 1
        self.active = True
        return True

    def stop(self) -> None:
        self.active = False


async def until(check, label: str, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out: {label}")
        await asyncio.sleep(0.02)


@pytest.fixture
def ilink():
    fake = FakeIlink()
    yield fake
    fake.close()


def client(fake: FakeIlink) -> IlinkClient:
    return IlinkClient(base_url=fake.base, cdn_url=fake.cdn)


async def logged_in(fake: FakeIlink, store: WeixinStore):
    shown, reports = [], []

    async def show(link):
        shown.append(link)

    async def report(text):
        reports.append(text)

    login = client(fake)
    try:
        account = await qr_login(login, store, show=show, report=report, interval=0.01)
    finally:
        await login.close()
    return account, shown, reports


def adapter(fake, store, handler, *, awake=None, **config):
    return WeixinAdapter(WeixinConfig(enabled=True, **config), handler, store=store, client=client(fake),
                         keep_awake=awake or FakeAwake(), owner_retry=0.05, failure_delays=(0.01, 0.01),
                         pause=lambda _text: 0.0)


def test_a_scanned_login_is_kept_privately_and_the_bot_then_answers_its_owner(ilink, tmp_path):
    async def scenario():
        store = WeixinStore(tmp_path / "weixin")
        account, shown, reports = await logged_in(ilink, store)
        assert shown == [LOGIN_LINK] and reports == ["已扫码，请在微信里确认登录。"]
        assert account.user_id == OWNER and BOT_TOKEN not in repr(account)
        if os.name != "nt":
            assert (tmp_path / "weixin" / "account.json").stat().st_mode & 0o777 == 0o600

        seen = []

        async def handler(message, reply):
            seen.append(message)
            return f"收到：{message.text}"

        channel = adapter(ilink, store, handler)
        await channel.start()
        try:
            await until(lambda: channel.running, "online")
            ilink.inbound.put(ilink.text_message("你好", message_id="m-1", context_token="ctx-owner"))
            ilink.inbound.put(ilink.text_message("你是谁", sender=STRANGER, message_id="m-2"))
            ilink.inbound.put(ilink.text_message("你好", message_id="m-1", context_token="ctx-owner"))
            await until(lambda: ilink.replies_to(OWNER), "reply")
            await asyncio.sleep(0.4)
        finally:
            await channel.stop()

        # Only the person who scanned the code is answered, once per message.
        assert [(m.channel, m.sender_id, m.text) for m in seen] == [("weixin", OWNER, "你好")]
        assert ilink.replies_to(OWNER) == ["收到：你好"] and not ilink.replies_to(STRANGER)
        reply = next(m for m in ilink.sent if m["to_user_id"] == OWNER)
        assert reply["context_token"] == "ctx-owner"
        authorized = [h for h in ilink.headers if h.get("Authorization")]
        assert authorized and all(h["Authorization"] == f"Bearer {BOT_TOKEN}" and h["AuthorizationType"] == "ilink_bot_token"
                                  and h.get("X-WECHAT-UIN") for h in authorized)

        # The cursor survives: the next poller resumes after the delivered message.
        last = f"cursor-{ilink._cursor}"
        assert ilink._cursor >= 1 and store.sync_buf() == last
        again = adapter(ilink, store, handler)
        await again.start()
        try:
            await until(lambda: len(ilink.polls) and ilink.polls[-1].get("get_updates_buf") == last, "resumed")
        finally:
            await again.stop()

    asyncio.run(scenario())


def test_allow_users_widens_who_is_answered(ilink, tmp_path):
    async def scenario():
        store = WeixinStore(tmp_path / "weixin")
        await logged_in(ilink, store)

        async def handler(message, reply):
            return "ok"

        channel = adapter(ilink, store, handler, allow_users=(OWNER, STRANGER))
        await channel.start()
        try:
            ilink.inbound.put(ilink.text_message("hi", sender=STRANGER))
            await until(lambda: ilink.replies_to(STRANGER), "stranger answered")
        finally:
            await channel.stop()

    asyncio.run(scenario())


def test_a_long_reply_is_split_and_a_rate_limited_piece_is_sent_again(ilink, tmp_path):
    async def scenario():
        store = WeixinStore(tmp_path / "weixin")
        await logged_in(ilink, store)
        long_answer = "\n".join(f"第 {i} 行：" + "内容" * 20 for i in range(30))

        async def handler(message, reply):
            return long_answer

        ilink.send_answers.put({"ret": -2, "errmsg": "frequency limit"})
        channel = adapter(ilink, store, handler, max_output_chars=300)
        await channel.start()
        try:
            ilink.inbound.put(ilink.text_message("长一点"))
            await until(lambda: "\n".join(ilink.replies_to(OWNER)[1:]) == long_answer, "all pieces", timeout=20)
        finally:
            await channel.stop()
        pieces = ilink.replies_to(OWNER)
        assert all(len(piece) <= 300 for piece in pieces)
        # The refused first piece was sent again, so nothing is missing or doubled.
        assert "\n".join(pieces[1:]) == long_answer and pieces[0] == pieces[1]

    asyncio.run(scenario())


def test_an_expired_login_stops_the_channel_and_asks_for_a_new_scan(ilink, tmp_path):
    async def scenario():
        store = WeixinStore(tmp_path / "weixin")
        await logged_in(ilink, store)
        awake = FakeAwake()

        async def handler(message, reply):
            return "ok"

        ilink.update_answers.put({"ret": -14, "errmsg": "session expired"})
        channel = adapter(ilink, store, handler, awake=awake)
        await channel.start()
        await until(lambda: channel.status()["state"] == "expired", "expired")
        polls = len(ilink.polls)
        await asyncio.sleep(0.2)
        assert len(ilink.polls) == polls, "an expired login is not polled again"
        status = channel.status()
        assert status["error"] == "微信登录已失效，请重新扫码登录。" and not status["owner"] and not awake.active
        await channel.stop()
        assert channel.status()["state"] == "expired"

    asyncio.run(scenario())


def test_only_one_backend_polls_a_login_and_another_takes_over_when_it_stops(ilink, tmp_path):
    async def scenario():
        store = WeixinStore(tmp_path / "weixin")
        await logged_in(ilink, store)

        async def handler(message, reply):
            return "ok"

        first_awake, second_awake = FakeAwake(), FakeAwake()
        first = adapter(ilink, store, handler, awake=first_awake)
        second = adapter(ilink, WeixinStore(tmp_path / "weixin"), handler, awake=second_awake)
        await first.start()
        await until(lambda: first.running, "first online")
        await second.start()
        await until(lambda: second.status()["state"] == "standby", "second waits")
        assert first_awake.active and not second_awake.active
        await first.stop()
        await until(lambda: second.running, "second takes over")
        assert second_awake.active and not first_awake.active
        await second.stop()
        assert not second_awake.active

    asyncio.run(scenario())


def test_images_are_decrypted_from_the_cdn_and_other_hosts_are_not_fetched(ilink, tmp_path):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    picture = b"\x89PNG\r\n\x1a\n" + bytes(range(64))
    key = bytes(range(16))
    padded = picture + bytes([16 - len(picture) % 16]) * (16 - len(picture) % 16)
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    ilink.media["img-1"] = encryptor.update(padded) + encryptor.finalize()

    async def scenario():
        store = WeixinStore(tmp_path / "weixin")
        await logged_in(ilink, store)
        seen = []

        async def handler(message, reply):
            seen.append(message)
            return "看到了"

        channel = adapter(ilink, store, handler)
        await channel.start()
        try:
            hex_key = base64.b64encode(key.hex().encode()).decode()
            ilink.inbound.put({**ilink.text_message("", message_id="i-1"), "item_list": [
                {"type": 2, "image_item": {"media": {"encrypt_query_param": "img-1", "aes_key": hex_key}}},
                {"type": 2, "image_item": {"media": {"full_url": "https://example.com/x.png"}}},
                {"type": 1, "text_item": {"text": "这是什么"}},
            ]})
            await until(lambda: seen, "image message")
        finally:
            await channel.stop()
        message = seen[0]
        assert message.text == "[图片：没能读取]\n这是什么"
        assert [image.data_url for image in message.images] == [
            "data:image/png;base64," + base64.b64encode(picture).decode()]
        assert ilink.media_requests == ["img-1"]

    asyncio.run(scenario())


@pytest.mark.skipif(sys.platform != "darwin", reason="caffeinate is macOS")
def test_on_a_mac_the_channel_holds_off_idle_sleep_only_while_it_polls(ilink, tmp_path):
    from agent.channels.keep_awake import KeepAwake

    def holders() -> list[str]:
        listing = subprocess.run(["ps", "-ax", "-o", "command="], capture_output=True, text=True).stdout
        return [line for line in listing.splitlines() if line.startswith("/usr/bin/caffeinate -i -w ")
                and line.split()[-1] == str(os.getpid())]

    async def scenario():
        store = WeixinStore(tmp_path / "weixin")
        await logged_in(ilink, store)

        async def handler(message, reply):
            return "ok"

        channel = adapter(ilink, store, handler, awake=KeepAwake("test"))
        await channel.start()
        await until(lambda: channel.running, "online")
        assert len(holders()) == 1 and channel.status()["keep_awake"]
        await channel.stop()
        await until(lambda: not holders(), "released")

    asyncio.run(scenario())


def test_a_reply_is_split_the_way_people_text():
    assert chat_bubbles("哈哈，我在呢。\n\n今天怎么样？", 1800) == ["哈哈，我在呢。", "今天怎么样？"]
    # A few short lines read as separate texts; a list stays one message.
    assert chat_bubbles("好呀\n我刚查了一下\n明天会下雨", 1800) == ["好呀", "我刚查了一下", "明天会下雨"]
    assert chat_bubbles("要买的：\n- 牛奶\n- 面包", 1800) == ["要买的：\n- 牛奶\n- 面包"]
    # WeChat shows Markdown as written, so headings and emphasis markers go.
    assert chat_bubbles("## 结果\n\n**明天**有雨。", 1800) == ["结果", "明天有雨。"]
    # A long paragraph is cut at sentence ends; a short one stays whole.
    long = "今天天气不错，适合出去走走。你上次说想去公园看看樱花，现在正好是花期，要不要周末一起计划一下？我可以帮你查一下哪个公园人少一点。"
    assert "".join(chat_bubbles(long, 1800)) == long and len(chat_bubbles(long, 1800)) == 3
    assert chat_bubbles("嗯嗯，早点休息吧。", 1800) == ["嗯嗯，早点休息吧。"]
    assert chat_bubbles("Sure! It will rain. It costs 3.5 yuan at example.com, so take an umbrella with you today.", 1800) == [
        "Sure! It will rain.", "It costs 3.5 yuan at example.com, so take an umbrella with you today."]
    # Never more than the limit allows: neighbours join, and no message exceeds the length cap.
    many = "\n\n".join(f"第{i}条" for i in range(20))
    bubbles = chat_bubbles(many, 1800, max_bubbles=5)
    assert len(bubbles) == 5 and "\n".join(bubbles).split("\n") == [f"第{i}条" for i in range(20)]
    assert all(len(b) <= 50 for b in chat_bubbles("字" * 400, 50))


def test_the_reply_goes_out_as_short_messages_while_wechat_shows_typing(ilink, tmp_path):
    async def scenario():
        store = WeixinStore(tmp_path / "weixin")
        await logged_in(ilink, store)

        async def handler(message, reply):
            return "哈哈，我在呢。\n\n今天怎么样？"

        channel = adapter(ilink, store, handler)
        await channel.start()
        try:
            ilink.inbound.put(ilink.text_message("在吗"))
            await until(lambda: ("typing", 2) in ilink.timeline, "typing stopped")
        finally:
            await channel.stop()
        texts = [value for kind, value in ilink.timeline if kind == "text"]
        assert texts == ["哈哈，我在呢。", "今天怎么样？"]
        first_text = next(i for i, (kind, _) in enumerate(ilink.timeline) if kind == "text")
        last_text = max(i for i, (kind, _) in enumerate(ilink.timeline) if kind == "text")
        assert ("typing", 1) in ilink.timeline[:first_text]
        assert ilink.timeline[-1] == ("typing", 2) and ilink.timeline.index(("typing", 2)) > last_text

    asyncio.run(scenario())


def test_only_wechat_conversations_are_told_to_talk_like_a_chat(tmp_path):
    prompts: dict[str, str] = {}

    class Agent:
        def __init__(self) -> None:
            self.context = AgentContext(system_prompt="system", max_messages=20, max_prompt_tokens=1000)
            self.context.set_stable_system_suffix("project guidance")
            self.llm = object()

        async def reply_stream(self, message):
            prompts[message.metadata["channel"]] = self.context.effective_system_prompt
            yield {"type": "chunk", "content": "ok"}
            yield {"type": "done", "content": "ok"}

    async def scenario():
        router = AgentChannelRouter(Agent(), asyncio.Lock(), tmp_path)
        await router.handle(ChannelMessage("weixin", OWNER, OWNER, "在吗"))
        await router.handle(ChannelMessage("qq", "peer", "peer", "hi"))

    asyncio.run(scenario())
    assert CHAT_STYLE in prompts["weixin"] and "project guidance" in prompts["weixin"]
    assert CHAT_STYLE not in prompts["qq"] and "project guidance" in prompts["qq"]
