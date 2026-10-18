"""WeChat over iLink: login, who is answered, replies, expiry, one poller, images, staying awake."""

import asyncio
import base64
import json
import os
import subprocess
import sys

import pytest
from weixin_fixture import BOT_TOKEN, LOGIN_LINK, OWNER, STRANGER, FakeIlink

from agent.channels.base import ChannelMessage
from agent.channels.config import WeixinConfig
from agent.channels.router import AgentChannelRouter
from agent.channels.weixin import CHAT_STYLE, IlinkClient, WeixinAdapter, WeixinError, WeixinStore, chat_bubbles, qr_login
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


def test_files_go_out_encrypted_as_an_image_or_a_file_and_only_to_the_owner(ilink, tmp_path):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    from agent.channels.manager import ChannelManager
    from agent.channels.router import active_channel_message
    from agent.channels.tools import register_channel_tools
    from agent.channels.config import ChannelsConfig
    from agent.runtime.tools.registry import ToolRegistry

    shared = tmp_path / "shared"
    (shared / "pictures").mkdir(parents=True)
    picture = shared / "pictures" / "cat.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(range(200)))
    notes = shared / "notes.txt"
    notes.write_text("买牛奶", encoding="utf-8")
    for hidden in (shared / ".astra" / "channels" / "weixin" / "account.json", shared / "Library" / "Keychains" / "login.db"):
        hidden.parent.mkdir(parents=True)
        hidden.write_text("secret", encoding="utf-8")

    async def scenario():
        store = WeixinStore(tmp_path / "weixin")
        await logged_in(ilink, store)

        async def handler(message, reply):
            return ""

        channel = adapter(ilink, store, handler, send_file_roots=(str(shared),))
        manager = ChannelManager(ChannelsConfig(weixin=channel.config), handler, adapters=[channel])
        registry = ToolRegistry()
        register_channel_tools(registry, lambda: manager)
        await channel.start()
        await until(lambda: channel.running, "online")

        async def send(path, sender=OWNER):
            token = active_channel_message.set(ChannelMessage("weixin", sender, sender, "发给我"))
            try:
                return await registry.execute("channel_send_file", {"path": str(path)})
            finally:
                active_channel_message.reset(token)

        try:
            results = {name: await send(path) for name, path in
                       (("picture", picture), ("notes", notes),
                        ("login", shared / ".astra" / "channels" / "weixin" / "account.json"),
                        ("keychain", shared / "Library" / "Keychains" / "login.db"))}
            results["stranger"] = await send(picture, sender=STRANGER)
        finally:
            await channel.stop()
        return results

    results = asyncio.run(scenario())
    assert results["picture"]["error"] == "" and results["notes"]["error"] == ""
    for refused in ("login", "keychain"):
        assert "sensitive" in results[refused]["error"], refused
    assert "not allowed" in results["stranger"]["error"]

    image, document = (message["item_list"][0] for message in ilink.sent)
    assert [message["to_user_id"] for message in ilink.sent] == [OWNER, OWNER]
    assert image["type"] == 2 and document["type"] == 4
    assert document["file_item"]["file_name"] == "notes.txt" and document["file_item"]["len"] == str(len("买牛奶".encode()))
    assert [r["media_type"] for r in ilink.upload_requests] == [1, 3]
    # What reached the CDN decrypts, with the key the message carries, to the file itself.
    for item, source in ((image["image_item"], picture), (document["file_item"], notes)):
        key = bytes.fromhex(base64.b64decode(item["media"]["aes_key"]).decode())
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        plain = decryptor.update(ilink.uploads[item["media"]["encrypt_query_param"]]) + decryptor.finalize()
        assert plain[:-plain[-1]] == source.read_bytes()


def test_stopping_finishes_even_when_an_http_call_swallows_the_cancellation(ilink, tmp_path):
    import httpx

    class SwallowingClient(IlinkClient):
        """What an HTTP stack can do: turn a cancellation into an ordinary error."""

        async def send_typing(self, account, to, ticket, status):
            try:
                await asyncio.sleep(0.05)
                await super().send_typing(account, to, ticket, status)
            except asyncio.CancelledError:
                raise httpx.ReadError("connection closed") from None

    async def scenario():
        store = WeixinStore(tmp_path / "weixin")
        await logged_in(ilink, store)
        started = asyncio.Event()

        async def handler(message, reply):
            started.set()
            while True:  # a turn still running when the desktop quits
                try:
                    await asyncio.sleep(0.01)
                except asyncio.CancelledError:
                    raise httpx.ReadError("connection closed") from None

        channel = WeixinAdapter(WeixinConfig(enabled=True), handler, store=store,
                                client=SwallowingClient(base_url=ilink.base, cdn_url=ilink.cdn),
                                keep_awake=FakeAwake(), owner_retry=0.05, failure_delays=(0.01, 0.01),
                                pause=lambda _text: 0.0)
        await channel.start()
        await until(lambda: channel.running, "online")
        ilink.inbound.put(ilink.text_message("在吗"))
        await asyncio.wait_for(started.wait(), 5)
        loop = asyncio.get_running_loop()
        began = loop.time()
        await asyncio.wait_for(channel.stop(), 20)
        assert loop.time() - began < 12
        assert channel.status()["state"] == "stopped"

    asyncio.run(scenario())


def test_a_voice_note_is_spoken_and_sent_as_audio_and_needs_no_text(ilink, tmp_path):
    import math
    import struct

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    from agent.channels.config import ChannelsConfig
    from agent.channels.manager import ChannelManager
    from agent.channels.router import active_channel_message
    from agent.channels.tools import register_channel_tools
    from agent.runtime.tools.registry import ToolRegistry

    rate = 24000
    tone = b"".join(struct.pack("<h", int(6000 * math.sin(2 * math.pi * 330 * i / rate))) for i in range(rate // 2))
    spoken: list[str] = []

    async def speaker(text):
        spoken.append(text)
        return tone, rate

    async def scenario():
        store = WeixinStore(tmp_path / "weixin")
        await logged_in(ilink, store)

        async def handler(message, reply):
            return ""

        channel = adapter(ilink, store, handler)
        manager = ChannelManager(ChannelsConfig(weixin=channel.config), handler, adapters=[channel])
        registry = ToolRegistry()
        register_channel_tools(registry, lambda: manager, speaker=speaker)
        await channel.start()
        await until(lambda: channel.running, "online")

        async def call(arguments, channel_name="weixin", sender=OWNER):
            token = active_channel_message.set(ChannelMessage(channel_name, sender, sender, "用语音说"))
            try:
                return await registry.execute("channel_send_file", arguments)
            finally:
                active_channel_message.reset(token)

        try:
            sent = await call({"speak": "**晚安**，早点休息 😴"})
            both = await call({"speak": "你好", "path": str(tmp_path)})
            neither = await call({})
        finally:
            await channel.stop()
        return sent, both, neither

    sent, both, neither = asyncio.run(scenario())
    assert sent["error"] == "" and spoken == ["晚安，早点休息"], "markup and emoji are not read out"
    assert "either path" in both["error"] and "either path" in neither["error"]
    note = ilink.sent[-1]["item_list"][0]
    assert note["type"] == 4 and note["file_item"]["file_name"].startswith("语音 ")
    key = bytes.fromhex(base64.b64decode(note["file_item"]["media"]["aes_key"]).decode())
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    audio = decryptor.update(ilink.uploads[note["file_item"]["media"]["encrypt_query_param"]]) + decryptor.finalize()
    audio = audio[:-audio[-1]]
    if sys.platform == "darwin":
        assert note["file_item"]["file_name"].endswith(".m4a") and audio[4:8] == b"ftyp"
    else:
        assert note["file_item"]["file_name"].endswith(".wav") and audio[:4] == b"RIFF"


def test_a_turn_that_answered_with_a_voice_note_alone_sends_no_placeholder(tmp_path):
    from agent.channels.router import channel_turn_media

    class Agent:
        def __init__(self) -> None:
            self.context = AgentContext(system_prompt="system", max_messages=20, max_prompt_tokens=1000)
            self.llm = object()

        async def reply_stream(self, message):
            channel_turn_media.get().append("voice")  # what channel_send_file records
            yield {"type": "done", "content": ""}

    async def scenario():
        router = AgentChannelRouter(Agent(), asyncio.Lock(), tmp_path)
        return await router.handle(ChannelMessage("weixin", OWNER, OWNER, "用语音说晚安"))

    assert asyncio.run(scenario()) == ""


def test_voice_notes_are_wechat_only(tmp_path):
    from agent.channels.config import ChannelsConfig, OneBotConfig
    from agent.channels.manager import ChannelManager
    from agent.channels.router import active_channel_message
    from agent.channels.tools import register_channel_tools
    from agent.runtime.tools.registry import ToolRegistry

    async def scenario():
        async def handler(message, reply=None):
            return ""

        manager = ChannelManager(ChannelsConfig(onebot=OneBotConfig(send_files_enabled=True, file_allow_users=("10001",),
                                                                   send_file_roots=(str(tmp_path),))), handler, adapters=[])
        registry = ToolRegistry()
        register_channel_tools(registry, lambda: manager, speaker=None)
        token = active_channel_message.set(ChannelMessage("qq", "10001", "10001", "发语音"))
        try:
            return await registry.execute("channel_send_file", {"speak": "你好"})
        finally:
            active_channel_message.reset(token)

    assert "only available in WeChat" in asyncio.run(scenario())["error"]


def test_a_wechat_turn_runs_on_the_channel_model_and_hands_the_window_its_own_back(tmp_path):
    from types import SimpleNamespace

    seen: dict[str, tuple] = {}
    window_llm = SimpleNamespace(config=SimpleNamespace(context_limit=None))
    chat_llm = SimpleNamespace(config=SimpleNamespace(context_limit=32_768, max_tokens=4096, model="small",
                                                      provider="openai-compatible", auto_compact_token_limit=None))

    class Agent:
        def __init__(self) -> None:
            self.context = AgentContext(system_prompt="system", max_messages=20, max_prompt_tokens=100_000)
            self.llm = window_llm

        async def reply_stream(self, message):
            seen[message.metadata["channel"]] = (self.llm, self.context.compressor.llm, self.context.max_prompt_tokens)
            yield {"type": "done", "content": "ok"}

    agent = Agent()
    router = AgentChannelRouter(agent, asyncio.Lock(), tmp_path,
                                model_for=lambda channel: chat_llm if channel == "weixin" else None)

    async def scenario():
        await router.handle(ChannelMessage("weixin", OWNER, OWNER, "在吗"))
        await router.handle(ChannelMessage("qq", "peer", "peer", "hi"))

    asyncio.run(scenario())
    assert seen["weixin"][:2] == (chat_llm, chat_llm) and seen["weixin"][2] == 16_384
    assert seen["qq"] == (window_llm, window_llm, 100_000)
    assert agent.llm is window_llm


def test_the_wechat_model_setting_is_kept_beside_the_other_settings(tmp_path):
    from agent.channels.config import load_channels_config, set_channel_setting

    path = tmp_path / "channels.json"
    path.write_text(json.dumps({"weixin": {"enabled": True, "keep_awake": False}}), encoding="utf-8")
    set_channel_setting("weixin", "model", " deepseek::deepseek-flash ", path)
    config = load_channels_config(path).weixin
    assert (config.model, config.enabled, config.keep_awake) == ("deepseek::deepseek-flash", True, False)
    set_channel_setting("weixin", "model", "", path)
    assert load_channels_config(path).weixin.model == ""


def test_a_media_upload_tries_a_passing_server_error_again_and_says_why_it_gave_up(ilink, monkeypatch):
    from agent.channels import weixin
    from agent.channels.weixin import WeixinError

    monkeypatch.setattr(weixin, "_UPLOAD_PAUSE", 0)
    url = f"{ilink.cdn}/upload?encrypted_query_param=up-1&filekey=k"

    async def scenario():
        client = IlinkClient(base_url=ilink.base, cdn_url=ilink.cdn)
        try:
            ilink.upload_failures = [(500, {}, b"<html> busy </html>")]
            assert await client.upload(url, b"x" * 16) == "enc-1"
            assert ilink.upload_attempts == 2

            ilink.upload_failures = [(500, {"x-error-message": "upload busy"}, b"")] * 3
            with pytest.raises(WeixinError, match=r"failed after 3 tries \(HTTP 500: upload busy\)"):
                await client.upload(url, b"x" * 16)
            assert ilink.upload_attempts == 5

            ilink.upload_failures = [(400, {"x-error-message": "bad filekey"}, b"")]
            with pytest.raises(WeixinError, match=r"was refused \(HTTP 400: bad filekey\)"):
                await client.upload(url, b"x" * 16)
            assert ilink.upload_attempts == 6, "a refusal is not tried again"
            assert list(ilink.uploads) == ["enc-1"]
        finally:
            await client.close()

    asyncio.run(scenario())


def test_sending_a_file_is_not_journaled_as_changing_it(tmp_path):
    from types import SimpleNamespace

    from agent.channels.base import FilePolicy
    from agent.channels.router import active_channel_message
    from agent.channels.tools import register_channel_tools
    from agent.runtime.react import ReActAgent
    from agent.runtime.tools.registry import ToolDef, ToolRegistry

    picture = tmp_path / "cat.png"
    picture.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes(64))
    sent: list[str] = []

    class Manager:
        def file_policy(self, channel):
            return FilePolicy(enabled=True, recipients=(OWNER,), roots=(str(tmp_path),), max_bytes=1 << 20)

        async def send_file(self, message, path, name):
            sent.append(path)
            return {"status": "sent"}

    async def edit_probe(path=""):
        return path

    registry = ToolRegistry()
    register_channel_tools(registry, lambda: Manager())
    registry.register(ToolDef(name="edit_probe", description="test mutation", fn=edit_probe, risk="write", group="files",
                              parameters={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}))
    llm = SimpleNamespace(config=SimpleNamespace(model="fake", capabilities=frozenset()))
    agent = ReActAgent("agent", llm, registry, system_prompt="base")  # type: ignore[arg-type]

    async def scenario():
        token = active_channel_message.set(ChannelMessage("weixin", OWNER, OWNER, "把图发我"))
        try:
            shared = await agent._execute_tool_call({"id": "send", "name": "channel_send_file",
                                                     "arguments": json.dumps({"path": str(picture)})})
            required_after_send = agent._verification_required
            edited = await agent._execute_tool_call({"id": "edit", "name": "edit_probe", "arguments": '{"path":"notes.md"}'})
        finally:
            active_channel_message.reset(token)
        return shared, required_after_send, edited

    shared, required_after_send, edited = asyncio.run(scenario())
    assert shared["error"] == "" and sent == [str(picture.resolve())]
    assert "coding_change_journal" not in shared and "Coding change journal" not in shared.get("tool_output", "")
    assert required_after_send is False
    assert edited["coding_change_journal"] == ["notes.md"], "a tool that writes its path is still journaled"


def test_while_an_approval_waits_only_the_askers_approval_word_answers_it(ilink, tmp_path):
    from agent.channels.approvals import approval_reply

    answers: list[str] = []
    turns: list[str] = []
    decided = asyncio.Event()

    async def scenario():
        store = WeixinStore(tmp_path / "weixin")
        await logged_in(ilink, store)

        async def handler(message, reply):
            turns.append(message.text)
            if message.text == "写文件":
                def answer(decision):
                    answers.append(decision)
                    decided.set()
                await channel.ask_approval(message, "需要你批准：写文件", answer)
                await decided.wait()
                await channel.end_approval(message)
            return ""

        channel = adapter(ilink, store, handler, allow_users=(OWNER, STRANGER))
        await channel.start()
        await until(lambda: channel.running, "online")
        try:
            ilink.inbound.put(ilink.text_message("写文件"))
            await until(lambda: ilink.replies_to(OWNER) == ["需要你批准：写文件"], "the request reaches the chat")
            ilink.inbound.put(ilink.text_message("好的我看看"))
            ilink.inbound.put(ilink.text_message("同意", sender=STRANGER))
            ilink.inbound.put(ilink.text_message("同意！", context_token="ctx-yes"))
            await until(lambda: answers == ["once"], "the owner's answer")
            await until(lambda: len(turns) == 3, "the other two messages are ordinary turns")
        finally:
            await channel.stop()

    asyncio.run(scenario())
    assert turns == ["写文件", "好的我看看", "同意"], "neither the chat nor a stranger's word answered it"
    assert answers == ["once"]
    assert [approval_reply(word) for word in ("拒绝", " no ", "同意。", "同意这个吗")] == ["deny", "deny", "once", None]


def test_no_answer_in_time_is_a_denial_the_person_hears_about_and_keys_stay_hidden():
    from agent.channels.approvals import await_chat_approval

    said: list[tuple[str, str]] = []

    class Chat:
        async def ask_approval(self, message, prompt, answer):
            said.append(("ask", prompt))

        async def end_approval(self, message, notice=""):
            said.append(("end", notice))

    async def scenario():
        decision = asyncio.get_running_loop().create_future()

        def resolve(value):
            if decision.done():
                return False
            decision.set_result(value)
            return True

        request = {"approval_title": "在受保护主机上运行 shell 命令", "reason": "该命令将在宿主机运行",
                   "target": "curl -H 'Authorization: Bearer abcdefgh12345678' https://example.com; "
                             "OPENAI=sk-proj1234abcdEFGHijklMNOP5678 GITHUB_TOKEN=ghp_zyxw9876 "
                             "git clone https://me:hunter22@example.com/r.git"}
        return await await_chat_approval(Chat(), ChannelMessage("weixin", OWNER, OWNER, "查一下"), request,
                                         decision, resolve, timeout=0.05)

    assert asyncio.run(scenario()) == "deny"
    (ask, prompt), (end, notice) = said
    assert (ask, end) == ("ask", "end") and prompt.startswith("需要你批准：在受保护主机上运行 shell 命令")
    for secret in ("abcdefgh12345678", "EFGHijklMNOP5678", "ghp_zyxw9876", "hunter22"):
        assert secret not in prompt, secret
    # What the command does stays readable.
    for kept in ("curl -H 'Authorization: Bearer …' https://example.com", "OPENAI=sk-proj1…", "GITHUB_TOKEN=…",
                 "git clone https://me:…@example.com/r.git"):
        assert kept in prompt, (kept, prompt)
    assert "没有执行" in notice


def test_an_approval_whose_request_could_not_be_sent_still_ends_and_answers_after_the_limit():
    from agent.channels.approvals import await_chat_approval

    calls: list[str] = []

    class Chat:
        async def ask_approval(self, message, prompt, answer):
            calls.append("ask")
            raise WeixinError("WeChat will not take a reply until this person writes to the bot again")

        async def end_approval(self, message, notice=""):
            calls.append(f"end:{bool(notice)}")

    async def scenario():
        decision = asyncio.get_running_loop().create_future()

        def resolve(value):
            if not decision.done():
                decision.set_result(value)
            return True

        return await await_chat_approval(Chat(), ChannelMessage("weixin", OWNER, OWNER, "查一下"),
                                         {"target": "ls"}, decision, resolve, timeout=0.05)

    assert asyncio.run(scenario()) == "deny"
    assert calls == ["ask", "end:True"]


def test_wechat_conversations_use_the_wechat_version_of_the_persona_and_keep_it(tmp_path, monkeypatch):
    from agent.cli import persona_preferences
    from agent.runtime.prompts import prompt_profiles, selectable_prompt_profiles

    variant = next(profile for profile in prompt_profiles().values() if profile.channel == "weixin")
    persona = prompt_profiles()[variant.name.removesuffix("-weixin")]
    prompts: dict[str, list[str]] = {"weixin": [], "qq": []}

    class Agent:
        def __init__(self) -> None:
            state = persona.default_state()
            self.context = AgentContext(system_prompt="", max_messages=20, max_prompt_tokens=100_000)
            self.context.set_persona(state, persona.system_prompt(state))
            self.llm = object()

        async def reply_stream(self, message):
            prompts[message.metadata["channel"]].append(self.context.system_prompt)
            self.context.add_user(message.get_text())
            self.context.add_assistant("ok")
            yield {"type": "done", "content": "ok"}

    async def talk(router):
        await router.handle(ChannelMessage("weixin", OWNER, OWNER, "在吗"))
        await router.handle(ChannelMessage("qq", "peer", "peer", "hi"))

    window = Agent()
    asyncio.run(talk(AgentChannelRouter(window, asyncio.Lock(), tmp_path)))
    # Opened again later (a restart): the conversation is already on the WeChat version and stays byte-identical.
    asyncio.run(talk(AgentChannelRouter(Agent(), asyncio.Lock(), tmp_path)))

    first, again = prompts["weixin"]
    assert first == again == variant.system_prompt(variant.default_state())
    assert prompts["qq"] == [window.context.system_prompt] * 2, "other channels keep the selected persona"
    assert variant.name not in selectable_prompt_profiles() and persona.name in selectable_prompt_profiles()
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    with pytest.raises(ValueError):
        persona_preferences.save_selected_persona(variant.name)
