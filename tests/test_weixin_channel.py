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
        # A placeholder is not the person's words: it comes along beside them.
        assert (message.text, message.attachments) == ("这是什么", "[图片：没能读取]")
        assert [image.data_url for image in message.images] == [
            "data:image/png;base64," + base64.b64encode(picture).decode()]
        assert ilink.media_requests == ["img-1"]

    asyncio.run(scenario())


MEDIA_KEY = bytes(range(16))


def _sealed(data: bytes, key: bytes = MEDIA_KEY) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    pad = 16 - len(data) % 16
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(data + bytes([pad]) * pad) + encryptor.finalize()


def _on_cdn(fake: FakeIlink, param: str, data: bytes, key: bytes = MEDIA_KEY) -> None:
    fake.media[param] = _sealed(data, key)


def _media(param: str) -> dict:
    # WeChat's form of the key: base64 of its hex digits.
    return {"encrypt_query_param": param, "aes_key": base64.b64encode(MEDIA_KEY.hex().encode()).decode()}


def _file(param: str, name: str, size: int | None = None) -> dict:
    payload: dict = {"media": _media(param), "file_name": name}
    if size is not None:
        payload["len"] = str(size)
    return {"type": 4, "file_item": payload}


def _video(param: str, **declared) -> dict:
    return {"type": 5, "video_item": {"media": _media(param), **declared}}


def _received(fake: FakeIlink, tmp_path, messages: list[list[dict]], **config) -> list[ChannelMessage]:
    async def scenario():
        store = WeixinStore(tmp_path / "weixin")
        await logged_in(fake, store)
        seen: list[ChannelMessage] = []

        async def handler(message, reply):
            seen.append(message)
            return "收到"

        channel = adapter(fake, store, handler, **config)
        await channel.start()
        try:
            for items in messages:
                fake.inbound.put({**fake.text_message(""), "item_list": items})
            await until(lambda: len(seen) == len(messages), "received messages")
        finally:
            await channel.stop()
        return seen

    return asyncio.run(scenario())


def _saved(inbox) -> dict[str, bytes]:
    return {str(path.relative_to(inbox)).replace(os.sep, "/"): path.read_bytes()
            for path in sorted(inbox.rglob("*")) if path.is_file()}


def test_a_received_file_is_saved_for_the_person_and_a_short_text_file_is_read_in(ilink, tmp_path):
    notes = "# 状态机\r\n第一行\r\n第二行\n".encode()
    again = "同名的另一份".encode()
    report = b"%PDF-1.7\n" + bytes(range(256))
    code = b"    first = 1\n    second = 2  "
    wide = "宽字符的笔记".encode("utf-16")
    _on_cdn(ilink, "md-1", notes)
    _on_cdn(ilink, "pdf-1", report)
    _on_cdn(ilink, "md-2", again)
    _on_cdn(ilink, "odd-1", b"plain")
    _on_cdn(ilink, "py-1", code)
    _on_cdn(ilink, "u16-1", wide)
    inbox = tmp_path / "inbox"

    first, second, third, fourth, fifth = _received(ilink, tmp_path, [
        [{"type": 1, "text_item": {"text": "看看这两个"}}, _file("md-1", "06_笔记.md", len(notes)),
         _file("pdf-1", "report.pdf")],
        [_file("md-2", "06_笔记.md")],
        [_file("odd-1", "../../.secret\n.md"), _file("py-1", "com¹.py", len(code))],
        [_file("u16-1", "宽.txt")],
        # The first file again, quoted: the copy already saved is the one named.
        [{"type": 1, "text_item": {"text": "看看这份"},
          "ref_msg": {"title": "06_笔记.md", "message_item": _file("md-1", "06_笔记.md", len(notes))}}],
    ], receive_files_dir=str(inbox), max_input_chars=4)

    day = next(inbox.iterdir()).name
    assert _saved(inbox) == {f"{day}/06_笔记.md": notes, f"{day}/06_笔记 (2).md": again,
                             f"{day}/report.pdf": report, f"{day}/secret_.md": b"plain",
                             f"{day}/_com¹.py": code, f"{day}/宽.txt": wide}
    folder = inbox / day
    notes_block = "\n".join([
        f"[文件: 06_笔记.md（{len(notes)} B），已保存到 {folder / '06_笔记.md'}]",
        "[文件内容开始: 06_笔记.md]", "# 状态机", "第一行", "第二行", "[文件内容结束: 06_笔记.md]"])
    # What the person typed keeps its limit and stays apart from what they sent.
    assert (first.text, first.attachments) == ("看看这两", "\n".join([
        notes_block, f"[文件: report.pdf（{len(report)} B），已保存到 {folder / 'report.pdf'}]"]))
    assert (second.text, second.attachments.split("\n")[0]) == (
        "", f"[文件: 06_笔记 (2).md（{len(again)} B），已保存到 {folder / '06_笔记 (2).md'}]")
    # A code file keeps its indentation and trailing spaces; only the markers are added around it.
    assert third.attachments.split("\n")[4:] == [
        f"[文件: _com¹.py（{len(code)} B），已保存到 {folder / '_com¹.py'}]",
        "[文件内容开始: _com¹.py]", "    first = 1", "    second = 2  ", "[文件内容结束: _com¹.py]"]
    assert third.attachments.startswith(f"[文件: secret_.md（5 B），已保存到 {folder / 'secret_.md'}]\n")
    assert "\n宽字符的笔记\n" in fourth.attachments
    # A quote has its own limit (max_input_chars, 4 here) and comes beside the person's words, with the file.
    assert (fifth.text, fifth.attachments) == ("看看这份", "[引用: 06_笔]\n" + notes_block)


def test_only_a_file_that_arrived_whole_is_saved_and_the_limit_is_kept(ilink, tmp_path):
    import hashlib
    import re

    clip = b"\x00\x00\x00\x18ftypmp42" + bytes(88)
    _on_cdn(ilink, "big-1", bytes(5000))
    _on_cdn(ilink, "big-2", bytes(2000))
    _on_cdn(ilink, "short-1", b"fourteen bytes")
    _on_cdn(ilink, "other-key", b"opened with another key", key=bytes(range(16, 32)))
    _on_cdn(ilink, "vid-1", clip)
    _on_cdn(ilink, "vid-2", clip)
    _on_cdn(ilink, "vid-3", clip)
    # 16 bytes encrypt to 32: a len of 32 is the encrypted size, not the file's.
    _on_cdn(ilink, "pad-1", bytes(16))
    _on_cdn(ilink, "vid-k", clip)
    ilink.media["raw-1"] = b"plain"
    inbox = tmp_path / "inbox"

    (message,) = _received(ilink, tmp_path, [[
        _file("big-1", "big.zip", 5000),
        _file("big-2", "quiet.bin"),
        _file("gone-1", "gone.txt"),
        {"type": 4, "file_item": {"file_name": "nowhere.txt", "media": {"full_url": "https://example.com/x"}}},
        _file("short-1", "short.txt", 123),
        _file("other-key", "garbled.txt"),
        _video("vid-big", video_size="9000"),
        _video("vid-2", video_md5="0" * 32),
        _file("pad-1", "padded.txt", 32),
        _video("vid-3", video_size=str(len(clip))),
        _file("never-1", "odd-size.txt", "12 KB"),
        _video("never-2", video_md5="not-an-md5"),
        _video("never-3", video_md5=False),
        _video("never-4", video_md5=0),
        {"type": 5, "video_item": {"media": {"encrypt_query_param": "vid-k", "encrypt_type": 1}}},
        {"type": 4, "file_item": {"media": {"encrypt_query_param": "raw-1"}, "file_name": "raw.txt", "len": "5"}},
        _video("vid-1", video_size=str(len(_sealed(clip))), video_md5=hashlib.md5(clip).hexdigest()),
    ]], receive_files_dir=str(inbox), max_receive_file_bytes=1024)

    lines = message.attachments.split("\n")
    assert lines[:15] == [
        "[文件: big.zip（4.9 KB），超过 1.0 KB 的上限，没有下载]",
        "[文件: quiet.bin，超过 1.0 KB 的上限，没有下载]",
        "[文件: gone.txt，没能下载]",
        "[文件: nowhere.txt，没能下载]",
        "[文件: short.txt，下载的内容不完整或解不开，没有保存]",
        "[文件: garbled.txt，下载的内容不完整或解不开，没有保存]",
        "[视频（约 8.8 KB），超过 1.0 KB 的上限，没有下载]",
        "[视频，下载的内容不完整或解不开，没有保存]",
        "[文件: padded.txt，下载的内容不完整或解不开，没有保存]",
        "[视频，下载的内容不完整或解不开，没有保存]",
        "[文件: odd-size.txt，微信给出的大小或校验值格式不对，没有下载]",
        "[视频，微信给出的大小或校验值格式不对，没有下载]",
        "[视频，微信给出的大小或校验值格式不对，没有下载]",
        "[视频，微信给出的大小或校验值格式不对，没有下载]",
        # Marked as encrypted, but no key: never kept as encrypted bytes.
        "[视频，下载的内容不完整或解不开，没有保存]",
    ]
    # Media WeChat did not encrypt is kept as it came.
    assert lines[15:19] == [f"[文件: raw.txt（5 B），已保存到 {inbox / next(inbox.iterdir()).name / 'raw.txt'}]",
                            "[文件内容开始: raw.txt]", "plain", "[文件内容结束: raw.txt]"]
    video = re.fullmatch(r"\[视频: (视频-\d{6}\.mp4)（100 B），已保存到 (.+)\]", lines[19])
    assert video is not None and len(lines) == 20 and message.text == ""
    day = next(inbox.iterdir()).name
    assert _saved(inbox) == {f"{day}/{video.group(1)}": clip, f"{day}/raw.txt": b"plain"}
    assert video.group(2) == str(inbox / day / video.group(1))
    # A declared size over the limit, a malformed size or checksum, and a missing key are never fetched;
    # no host outside WeChat's CDN is asked.
    assert ilink.media_requests == ["big-2", "gone-1", "short-1", "other-key", "vid-2", "pad-1", "vid-3",
                                    "raw-1", "vid-1"]


def test_a_saved_file_never_takes_a_name_windows_would_refuse(ilink, tmp_path):
    for param in ("dev-1", "dev-2", "dev-3", "long-1", "long-2", "long-3", "long-4"):
        _on_cdn(ilink, param, param.encode())
    inbox = tmp_path / "inbox"
    # Cut to the name limit, these would end in spaces.
    _received(ilink, tmp_path, [[_file("dev-1", "CON .txt"), _file("dev-2", "conin$.log"),
                                 _file("dev-3", "lpt².tar.gz"), _file("long-1", "a" * 175 + " " * 10 + "z"),
                                 _file("long-2", "b" * 175 + " . " * 4 + "z.txt"),
                                 # Cut and trimmed, these would be a device name.
                                 _file("long-3", "CON" + " " * 200 + "x.txt"),
                                 _file("long-4", "LPT1" + " " * 200 + "y.csv")]], receive_files_dir=str(inbox))
    day = next(inbox.iterdir()).name
    assert _saved(inbox) == {f"{day}/_CON .txt": b"dev-1", f"{day}/_conin$.log": b"dev-2",
                             f"{day}/_lpt².tar.gz": b"dev-3", f"{day}/{'a' * 175}": b"long-1",
                             f"{day}/{'b' * 175}.txt": b"long-2", f"{day}/_CON.txt": b"long-3",
                             f"{day}/_LPT1.csv": b"long-4"}


def test_quotes_and_placeholders_never_crowd_out_or_stand_in_for_the_persons_words(ilink, tmp_path):
    quote = "这是一段很长的引用标题，比允许的输入长度还要长得多"
    (reply, picture, voice) = _received(ilink, tmp_path, [
        [{"type": 1, "text_item": {"text": "晚点提醒我喝茶"}, "ref_msg": {"title": quote}}],
        [{"type": 2, "image_item": {"media": {"full_url": "https://example.com/x.png"}}}],
        [{"type": 3, "voice_item": {}}],
    ], max_input_chars=10)
    assert (reply.text, reply.attachments) == ("晚点提醒我喝茶", f"[引用: {quote[:10]}]")
    assert (picture.text, picture.attachments) == ("", "[图片：没能读取]")
    assert (voice.text, voice.attachments) == ("", "[语音]")


def test_a_folder_that_cannot_be_used_costs_the_file_but_never_the_message(ilink, tmp_path):
    _on_cdn(ilink, "md-1", b"notes")
    blocked = tmp_path / "a-file"
    blocked.write_text("not a folder", encoding="utf-8")
    words = {"type": 1, "text_item": {"text": "十分钟后提醒我喝茶"}}
    (message,) = _received(ilink, tmp_path, [[words, _file("md-1", "议程.md")]],
                           receive_files_dir=str(blocked / "inbox"))
    assert (message.text, message.attachments) == ("十分钟后提醒我喝茶", "[文件: 议程.md（5 B），下载了但没能保存]")


@pytest.mark.skipif(sys.platform == "win32", reason="~user expansion fails only on POSIX")
def test_a_home_folder_of_nobody_is_reported_with_the_file_and_the_words_still_arrive(ilink, tmp_path):
    _on_cdn(ilink, "md-1", b"notes")
    words = {"type": 1, "text_item": {"text": "在吗"}}
    (message,) = _received(ilink, tmp_path, [[words, _file("md-1", "议程.md")]],
                           receive_files_dir="~no-such-user-for-astra-tests/inbox")
    assert (message.text, message.attachments) == ("在吗", "[文件: 议程.md（5 B），下载了但没能保存]")


def test_files_can_be_left_on_wechats_side_and_folders_are_found_from_home(ilink, tmp_path):
    from pathlib import Path

    from agent.channels.weixin import received_files_root

    _on_cdn(ilink, "md-1", b"notes")
    inbox = tmp_path / "inbox"
    (message,) = _received(ilink, tmp_path, [[_file("md-1", "notes.md"), _video("md-1")]],
                           receive_files_dir=str(inbox), max_receive_file_bytes=0)
    assert (message.text, message.attachments) == ("", "[文件: notes.md]\n[视频]")
    assert ilink.media_requests == [] and not inbox.exists()

    assert WeixinConfig.from_dict({}).max_receive_file_bytes == 50 * 1024 * 1024
    assert WeixinConfig.from_dict({"max_receive_file_bytes": 0}).max_receive_file_bytes == 0
    home = Path.home()
    assert received_files_root(WeixinConfig()) == home / "Downloads" / "Astra" / "微信"
    assert received_files_root(WeixinConfig.from_dict({"receive_files_dir": " ~/收到的 "})) == home / "收到的"
    # A relative folder is under home, whichever folder the backend started in.
    assert received_files_root(WeixinConfig.from_dict({"receive_files_dir": "收件"})) == home / "收件"
    assert received_files_root(WeixinConfig(receive_files_dir=str(inbox))) == inbox


def test_a_setting_that_is_not_a_number_is_reported_and_downloads_nothing(tmp_path):
    from agent.channels.config import load_channels_config

    path = tmp_path / "channels.json"
    stop = "收到的文件先不下载，只收文件名；写成字节数（如 52428800）后生效。"
    for written in ("50 MB", "--5", "", False, 1.5, [52428800]):
        path.write_text(json.dumps({"qq": {"port": "two thousand"}, "weixin": {
            "max_receive_file_bytes": written, "max_input_chars": " 8000 ", "max_bubbles": 3.0, "max_images": 0,
            "max_image_bytes": None}}), encoding="utf-8")
        config = load_channels_config(path)
        # Set but not a whole number, even empty: nothing is downloaded, and the setting is named.
        assert config.weixin.max_receive_file_bytes == 0, written
        assert config.weixin.problems == (
            f"weixin.max_receive_file_bytes 应为整数，现在是 {json.dumps(written, ensure_ascii=False)}；{stop}",)
        # Plain digits, a whole float, 0 where it means none, and null (not set) still read as before.
        assert (config.weixin.max_input_chars, config.weixin.max_bubbles, config.weixin.max_images,
                config.weixin.max_image_bytes) == (8000, 3, 0, 10 * 1024 * 1024)
        assert config.onebot.port == 2280
        assert config.onebot.problems == ('qq.port 应为整数，现在是 "two thousand"；暂按 2280 处理。',)
    path.write_text(json.dumps({"weixin": {}}), encoding="utf-8")
    assert load_channels_config(path).weixin.max_receive_file_bytes == 50 * 1024 * 1024


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
        await router.handle(ChannelMessage("qq", "peer", "peer", "hi", local_tools=True))

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
        await router.handle(ChannelMessage("qq", "peer", "peer", "hi", local_tools=True))

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
        await router.handle(ChannelMessage("qq", "peer", "peer", "hi", local_tools=True))

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


def test_a_kaomoji_is_never_split_across_messages():
    reply = "先别想任何事了，喝口水，靠一会儿。好消息之后还压着一整天的会，你这情绪都被榨干了吧 (´;ω;`) 晚饭吃了没？没吃的话我盯着你去吃！！"
    bubbles = chat_bubbles(reply, limit=1800)
    assert any("(´;ω;`)" in bubble for bubble in bubbles), bubbles
    assert "".join(bubbles).replace(" ", "") == reply.replace(" ", "")
    assert any(bubble.endswith("盯着你去吃！！") for bubble in bubbles), "a run of marks stays with its sentence"
    assert chat_bubbles("Version 3.14 is out. It runs on Python 3.11 and fixes the parser; try it now please.", limit=1800)[0].startswith("Version 3.14 is out.")


def _desktop_host(fake, store, handler):
    return WeixinAdapter(WeixinConfig(enabled=True), handler, store=store, client=client(fake), keep_awake=FakeAwake(),
                         owner_retry=0.05, failure_delays=(0.01, 0.01), pause=lambda _text: 0.0, priority=True)


def test_the_desktop_channel_host_takes_wechat_over_and_gives_it_back_when_it_stops(ilink, tmp_path, monkeypatch):
    from agent.channels import weixin

    monkeypatch.setattr(weixin, "_HOST_CHECK_SECONDS", 0.05)
    answered: list[tuple[str, str]] = []

    async def scenario():
        store = WeixinStore(tmp_path / "weixin")
        await logged_in(ilink, store)

        def handler_for(name):
            async def handler(message, reply):
                answered.append((name, message.text))
                return f"{name}:{message.text}"
            return handler

        terminal = adapter(ilink, store, handler_for("terminal"))
        await terminal.start()
        await until(lambda: terminal.running, "an old terminal backend polls WeChat")
        ilink.inbound.put(ilink.text_message("before"))
        await until(lambda: ("terminal", "before") in answered, "the terminal answers")

        host = _desktop_host(ilink, WeixinStore(tmp_path / "weixin"), handler_for("host"))
        await host.start()
        await until(lambda: host.running, "the desktop's host takes over")
        assert terminal.status()["state"] == "standby"
        ilink.inbound.put(ilink.text_message("after"))
        await until(lambda: ("host", "after") in answered, "the host answers")
        await asyncio.sleep(0.3)
        assert terminal.status()["state"] == "standby", "it does not take WeChat back while the host runs"

        await host.stop()
        await until(lambda: terminal.running, "the terminal takes over again once the host is gone")
        await terminal.stop()

    asyncio.run(scenario())
    assert answered == [("terminal", "before"), ("host", "after")]


def test_a_turn_in_progress_is_answered_before_the_channel_is_handed_over(ilink, tmp_path, monkeypatch):
    from agent.channels import weixin

    monkeypatch.setattr(weixin, "_HOST_CHECK_SECONDS", 0.05)
    answered: list[tuple[str, str]] = []
    release = asyncio.Event()

    async def scenario():
        store = WeixinStore(tmp_path / "weixin")
        await logged_in(ilink, store)

        async def slow(message, reply):
            await release.wait()
            answered.append(("terminal", message.text))
            return "done"

        async def fast(message, reply):
            answered.append(("host", message.text))
            return "ok"

        terminal = adapter(ilink, store, slow)
        await terminal.start()
        await until(lambda: terminal.running, "online")
        ilink.inbound.put(ilink.text_message("一个长任务"))
        await until(lambda: terminal._busy, "the turn started")

        host = _desktop_host(ilink, WeixinStore(tmp_path / "weixin"), fast)
        await host.start()
        await asyncio.sleep(0.3)
        assert not host.running, "the host waits for the turn in progress"
        release.set()
        await until(lambda: host.running, "then it takes over")
        assert ilink.replies_to(OWNER) == ["done"]
        ilink.inbound.put(ilink.text_message("下一条"))
        await until(lambda: ("host", "下一条") in answered, "the host answers the next one")
        await host.stop()
        await terminal.stop()

    asyncio.run(scenario())
    assert answered == [("terminal", "一个长任务"), ("host", "下一条")], "each message answered once"
