"""WeChat (Weixin) personal-account channel over Tencent's iLink Bot API.

Login is a QR code scanned in WeChat, which yields a bot token. A long poll of
``getupdates`` delivers messages, and every reply echoes the sender's latest
``context_token``. Images arrive AES-128-ECB encrypted on WeChat's CDN.

Nothing here touches the WeChat desktop client or the screen. Only one backend
polls a token at a time (an owner lock in the state directory); the others wait
and take over when it exits. While this backend polls it keeps the computer from
idle sleep, so the channel keeps working with the display off.

Protocol details follow Hermes Agent's adapter (``gateway/platforms/weixin.py``,
MIT License, Copyright (c) 2025 Nous Research).
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import secrets
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
import wave
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from agent.runtime.instance_lock import InstanceAlreadyRunning, InstanceLock
from agent.runtime.json_preferences import update_preferences
from agent.runtime.network import active_proxy_for_url
from agent.runtime.paths import state_path

from .base import ChannelAdapter, ChannelImage, ChannelMessage, MessageHandler
from .config import WeixinConfig
from .keep_awake import KeepAwake


logger = logging.getLogger(__name__)

ILINK_BASE_URL = "https://ilinkai.weixin.qq.com"
CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
_APP_ID = "bot"
_CHANNEL_VERSION = "2.2.0"
_APP_CLIENT_VERSION = (2 << 16) | (2 << 8) | 0
_LONG_POLL_MS = 35_000
_API_SECONDS = 15.0
_SESSION_EXPIRED = -14
_RATE_LIMITED = -2
_ITEM_TEXT, _ITEM_IMAGE, _ITEM_VOICE, _ITEM_FILE, _ITEM_VIDEO = 1, 2, 3, 4, 5
# getuploadurl's media_type, numbered differently from the message items.
_MEDIA_IMAGE, _MEDIA_VIDEO, _MEDIA_FILE = 1, 2, 3
_IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})
_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".m4v", ".3gp"})
_MSG_TYPE_BOT = _MSG_STATE_FINISH = 2
_CDN_HOSTS = frozenset({
    "novac2c.cdn.weixin.qq.com", "ilinkai.weixin.qq.com", "wx.qlogo.cn", "thirdwx.qlogo.cn",
    "res.wx.qq.com", "mmbiz.qpic.cn", "mmbiz.qlogo.cn",
})
_SEEN_LIMIT = 512
_SEND_ATTEMPTS = 4
_TYPING_START, _TYPING_STOP = 1, 2
_TYPING_REFRESH_SECONDS = 4.0
_TYPING_TICKET_SECONDS = 600.0

# Added to the system prompt of WeChat conversations only (see AgentChannelRouter): it is a
# chat on a phone, mostly company rather than work.
CHAT_STYLE = """[微信聊天]
这段对话来自微信：对方在手机上和你聊天，以闲聊和陪伴为主，偶尔请你帮忙做点小事。像发微信一样回复：
- 口语、自然、简短。一条消息说一件事；想说几件事就用空行隔开，它们会作为几条消息依次发出。
- 微信不显示 Markdown：不要用标题、加粗、表格和代码块，少用列表，需要分点时直接换行。
- 闲聊就好好聊：接话、追问、表达感受都可以，不必每次总结或给建议；表情偶尔用，别堆砌。
- 需要做事（查资料、记东西、定提醒）照常用工具，做完用一两句话说结果，不贴长报告；真需要长内容时先说结论，再问要不要展开。
- 对方想听你的声音、让你发语音时，用 channel_send_file 的 speak 把要说的话说出来（只写念出来的话，不带表情和符号）。可以再配一两句短文字（比如“收到了吗”），但不要用文字重复语音里的内容。"""


class WeixinError(RuntimeError):
    """An iLink request failed; the message never contains the bot token."""


class WeixinLoginError(WeixinError):
    pass


@dataclass(frozen=True)
class WeixinAccount:
    account_id: str
    token: str = field(repr=False)
    base_url: str = ILINK_BASE_URL
    # The WeChat user who scanned the login code: by default the only one the bot answers.
    user_id: str = ""


def mask(value: str) -> str:
    value = str(value or "")
    return f"{value[:4]}…" if len(value) > 4 else value


class WeixinStore:
    """Login, poll cursor and per-peer reply tokens, private to this installation."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else state_path("channels", "weixin")

    @property
    def owner_lock(self) -> Path:
        return self.root / "owner.lock"

    def _read(self, name: str) -> dict:
        try:
            value = json.loads((self.root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write(self, name: str, data: dict) -> None:
        # Written through a private temporary file and an atomic rename; the file is owner-only.
        def replace(current: dict) -> None:
            current.clear()
            current.update(data)

        update_preferences(self.root / name, replace)

    def account(self) -> WeixinAccount | None:
        data = self._read("account.json")
        if not data.get("account_id") or not data.get("token"):
            return None
        return WeixinAccount(str(data["account_id"]), str(data["token"]),
                             str(data.get("base_url") or ILINK_BASE_URL), str(data.get("user_id") or ""))

    def save_account(self, account: WeixinAccount) -> None:
        self._write("account.json", {"account_id": account.account_id, "token": account.token,
                                     "base_url": account.base_url, "user_id": account.user_id})
        for name in ("sync.json", "context-tokens.json"):
            (self.root / name).unlink(missing_ok=True)

    def forget(self) -> None:
        for name in ("account.json", "sync.json", "context-tokens.json"):
            (self.root / name).unlink(missing_ok=True)

    def sync_buf(self) -> str:
        return str(self._read("sync.json").get("get_updates_buf") or "")

    def save_sync_buf(self, value: str) -> None:
        self._write("sync.json", {"get_updates_buf": value})

    def context_tokens(self) -> dict[str, str]:
        return {str(k): str(v) for k, v in self._read("context-tokens.json").items() if v}

    def save_context_tokens(self, tokens: dict[str, str]) -> None:
        self._write("context-tokens.json", dict(tokens))


class IlinkClient:
    """HTTP calls of the iLink Bot API. Redirects are refused; errors never name the token."""

    def __init__(self, *, base_url: str | None = None, cdn_url: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = (base_url or os.getenv("ASTRA_WEIXIN_BASE_URL") or ILINK_BASE_URL).rstrip("/")
        self.cdn_url = (cdn_url or os.getenv("ASTRA_WEIXIN_CDN_URL") or CDN_BASE_URL).rstrip("/")
        proxy = active_proxy_for_url(self.base_url) if transport is None else None
        self._http = httpx.AsyncClient(transport=transport, proxy=proxy, follow_redirects=False, trust_env=False,
                                       timeout=httpx.Timeout(_API_SECONDS, connect=10.0))

    async def close(self) -> None:
        await self._http.aclose()

    @staticmethod
    def _app_headers() -> dict[str, str]:
        return {"iLink-App-Id": _APP_ID, "iLink-App-ClientVersion": str(_APP_CLIENT_VERSION)}

    def _headers(self, token: str) -> dict[str, str]:
        uin = base64.b64encode(str(int.from_bytes(secrets.token_bytes(4), "big")).encode()).decode("ascii")
        return {**self._app_headers(), "Content-Type": "application/json", "AuthorizationType": "ilink_bot_token",
                "X-WECHAT-UIN": uin, "Authorization": f"Bearer {token}"}

    @staticmethod
    def _decode(response: httpx.Response, endpoint: str) -> dict:
        if response.status_code != 200:
            raise WeixinError(f"iLink {endpoint} answered HTTP {response.status_code}")
        try:
            data = response.json()
        except ValueError as exc:
            raise WeixinError(f"iLink {endpoint} answered with something that is not JSON") from exc
        if not isinstance(data, dict):
            raise WeixinError(f"iLink {endpoint} answered with an unexpected shape")
        return data

    async def _get(self, base_url: str, endpoint: str, timeout: float = _API_SECONDS) -> dict:
        response = await self._http.get(f"{base_url.rstrip('/')}/{endpoint}", headers=self._app_headers(), timeout=timeout)
        return self._decode(response, endpoint.split("?", 1)[0])

    async def _post(self, account: WeixinAccount, endpoint: str, payload: dict, timeout: float = _API_SECONDS) -> dict:
        body = json.dumps({**payload, "base_info": {"channel_version": _CHANNEL_VERSION}},
                          ensure_ascii=False, separators=(",", ":"))
        response = await self._http.post(f"{account.base_url.rstrip('/')}/{endpoint}", content=body.encode("utf-8"),
                                         headers=self._headers(account.token), timeout=timeout)
        return self._decode(response, endpoint)

    async def bot_qrcode(self) -> tuple[str, str]:
        """The code to poll and the link WeChat must scan (encode the link, not the bare code)."""
        data = await self._get(self.base_url, "ilink/bot/get_bot_qrcode?bot_type=3")
        return str(data.get("qrcode") or ""), str(data.get("qrcode_img_content") or "")

    async def qrcode_status(self, base_url: str, qrcode: str) -> dict:
        return await self._get(base_url, f"ilink/bot/get_qrcode_status?qrcode={quote(qrcode, safe='')}",
                               timeout=_LONG_POLL_MS / 1000 + 5)

    async def get_updates(self, account: WeixinAccount, cursor: str, timeout_ms: int) -> dict:
        try:
            return await self._post(account, "ilink/bot/getupdates", {"get_updates_buf": cursor},
                                     timeout=timeout_ms / 1000 + 5)
        except httpx.ReadTimeout:
            return {"ret": 0, "msgs": [], "get_updates_buf": cursor}

    async def send_items(self, account: WeixinAccount, to: str, items: list[dict], context_token: str) -> dict:
        message: dict[str, Any] = {
            "from_user_id": "", "to_user_id": to, "client_id": f"astra-weixin-{uuid.uuid4().hex}",
            "message_type": _MSG_TYPE_BOT, "message_state": _MSG_STATE_FINISH, "item_list": items,
        }
        if context_token:
            message["context_token"] = context_token
        return await self._post(account, "ilink/bot/sendmessage", {"msg": message})

    async def send_text(self, account: WeixinAccount, to: str, text: str, context_token: str) -> dict:
        return await self.send_items(account, to, [{"type": _ITEM_TEXT, "text_item": {"text": text}}], context_token)

    async def upload_url(self, account: WeixinAccount, to: str, *, media_type: int, filekey: str, size: int,
                         md5: str, encrypted_size: int, aes_key_hex: str) -> str:
        """Where WeChat's CDN takes the encrypted file."""
        data = await self._post(account, "ilink/bot/getuploadurl", {
            "filekey": filekey, "media_type": media_type, "to_user_id": to, "rawsize": size, "rawfilemd5": md5,
            "filesize": encrypted_size, "no_need_thumb": True, "aeskey": aes_key_hex})
        full = str(data.get("upload_full_url") or "")
        param = str(data.get("upload_param") or "")
        if full:
            if urlparse(full).scheme not in {"http", "https"}:
                raise WeixinError("iLink handed out an upload address that is not HTTP")
            return full
        if param:
            return f"{self.cdn_url}/upload?encrypted_query_param={quote(param, safe='')}&filekey={quote(filekey, safe='')}"
        raise WeixinError("iLink handed out no upload address")

    async def upload(self, url: str, data: bytes) -> str:
        """Upload encrypted bytes; the CDN answers with the parameter the message refers to."""
        response = await self._http.post(url, content=data, headers={"Content-Type": "application/octet-stream"},
                                         timeout=120.0)
        param = response.headers.get("x-encrypted-param") if response.status_code == 200 else None
        if not param:
            raise WeixinError(f"WeChat's media upload answered HTTP {response.status_code} without a file reference")
        return param

    async def typing_ticket(self, account: WeixinAccount, to: str, context_token: str) -> str:
        payload: dict[str, Any] = {"ilink_user_id": to}
        if context_token:
            payload["context_token"] = context_token
        data = await self._post(account, "ilink/bot/getconfig", payload, timeout=10.0)
        return str(data.get("typing_ticket") or "")

    async def send_typing(self, account: WeixinAccount, to: str, ticket: str, status: int) -> None:
        await self._post(account, "ilink/bot/sendtyping",
                         {"ilink_user_id": to, "typing_ticket": ticket, "status": status}, timeout=10.0)

    async def download(self, url: str, max_bytes: int) -> bytes:
        async with self._http.stream("GET", url, timeout=60.0) as response:
            if response.status_code != 200:
                raise WeixinError(f"media download answered HTTP {response.status_code}")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise WeixinError(f"media is larger than {max_bytes} bytes")
            return bytes(data)


def qr_text(link: str) -> str:
    """The login code drawn with half blocks, for a terminal with a dark background."""
    import segno

    out = io.StringIO()
    segno.make(link, error="m").terminal(out=out, compact=True, border=2)
    return out.getvalue().rstrip("\n")


def qr_svg(link: str) -> str:
    import segno

    return segno.make(link, error="m").svg_data_uri(scale=6, border=2)


async def qr_login(client: IlinkClient, store: WeixinStore, *, show: Callable[[str], Awaitable[None]],
                   report: Callable[[str], Awaitable[None]], timeout: float = 480.0,
                   interval: float = 1.0) -> WeixinAccount:
    """Wait for the code to be scanned and confirmed in WeChat, then keep the login."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    code, link = await client.bot_qrcode()
    if not code or not link:
        raise WeixinLoginError("WeChat did not hand out a login code")
    await show(link)
    base_url, refreshed, scanned = client.base_url, 0, False
    while loop.time() < deadline:
        try:
            status = await client.qrcode_status(base_url, code)
        except (httpx.TimeoutException, httpx.TransportError, WeixinError) as exc:
            logger.info("weixin login status check failed: %s", type(exc).__name__)
            await asyncio.sleep(interval)
            continue
        state = str(status.get("status") or "wait")
        if state == "scaned" and not scanned:
            scanned = True
            await report("已扫码，请在微信里确认登录。")
        elif state == "scaned_but_redirect" and status.get("redirect_host"):
            base_url = f"https://{status['redirect_host']}"
        elif state == "expired":
            refreshed += 1
            if refreshed > 3:
                raise WeixinLoginError("二维码多次过期，请重新登录。")
            code, link = await client.bot_qrcode()
            scanned = False
            await show(link)
        elif state == "confirmed":
            account = WeixinAccount(str(status.get("ilink_bot_id") or ""), str(status.get("bot_token") or ""),
                                    str(status.get("baseurl") or client.base_url), str(status.get("ilink_user_id") or ""))
            if not account.account_id or not account.token:
                raise WeixinLoginError("WeChat confirmed the login without handing over a bot identity")
            store.save_account(account)
            return account
        await asyncio.sleep(interval)
    raise WeixinLoginError("登录超时，请重新扫码。")


def _aes_key(value: str) -> bytes:
    decoded = base64.b64decode(value)
    if len(decoded) == 16:
        return decoded
    text = decoded.decode("ascii", errors="ignore") if len(decoded) == 32 else ""
    if text and all(ch in "0123456789abcdefABCDEF" for ch in text):
        return bytes.fromhex(text)
    raise ValueError("unexpected media key format")


def _encrypt(data: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    pad = 16 - len(data) % 16
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(data + bytes([pad]) * pad) + encryptor.finalize()


def _decrypt(data: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    plain = decryptor.update(data) + decryptor.finalize()
    pad = plain[-1] if plain else 0
    return plain[:-pad] if 1 <= pad <= 16 and plain.endswith(bytes([pad]) * pad) else plain


def _image_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data.startswith(b"GIF8"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _stop_requested() -> bool:
    """Whether this task was asked to stop. An HTTP call can turn the cancellation into an ordinary
    error that a loop then logs and outlives, so loops check this as well as catching it."""
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


async def _finish(task: asyncio.Task, timeout: float) -> None:
    """Cancel a child task and wait for it, but never longer than ``timeout``."""
    task.cancel()
    await asyncio.wait({task}, timeout=timeout)


def split_reply(text: str, limit: int) -> list[str]:
    """Pieces of at most ``limit`` characters, cut at a paragraph or line break when one is near."""
    text = text.strip()
    pieces: list[str] = []
    while len(text) > limit:
        window = text[:limit]
        cut = max(window.rfind("\n\n"), window.rfind("\n"))
        cut = cut if cut >= limit // 2 else limit
        pieces.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        pieces.append(text)
    return pieces


_FENCE = re.compile(r"^\s*(```|~~~)")
_LIST_ITEM = re.compile(r"^\s*(?:[-*•·]|\d+[.、)）])\s+")
# A full stop ends a sentence only before a space, so decimals and addresses stay whole.
_SENTENCE_END = re.compile(r"(?<=[。！？!?…；;~～])\s*|(?<=\.)\s+")


def plain_text(text: str) -> str:
    """What WeChat shows as written: headings and emphasis markers removed, everything else kept."""
    lines = []
    in_code = False
    for line in text.splitlines():
        if _FENCE.match(line):
            in_code = not in_code
            continue
        if not in_code:
            line = re.sub(r"^\s{0,3}#{1,6}\s+", "", line)
            line = re.sub(r"(\*\*|__)(.+?)\1", r"\2", line)
        lines.append(line)
    return "\n".join(lines).strip()


def _chatty(lines: list[str]) -> bool:
    """A block that reads as a few short texts in a row, not a list or a paragraph."""
    return 2 <= len(lines) <= 6 and all(len(line.strip()) <= 40 and not _LIST_ITEM.match(line) for line in lines)


def chat_bubbles(text: str, limit: int, max_bubbles: int = 8, long_paragraph: int = 50, sentences_up_to: int = 30) -> list[str]:
    """Split a reply the way people text: a message per paragraph, a few short lines as
    separate messages, a long paragraph at sentence ends. Lists stay whole; nothing
    exceeds ``limit``; past ``max_bubbles`` neighbours are joined while they fit."""
    bubbles: list[str] = []
    for block in re.split(r"\n\s*\n", plain_text(text)):
        lines = [line for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if _chatty(lines):
            bubbles.extend(line.strip() for line in lines)
        elif len(lines) == 1 and len(lines[0]) > long_paragraph:
            sentences = [s for s in _SENTENCE_END.split(lines[0].strip()) if s]
            current = ""
            for sentence in sentences:
                if current and len(current) + len(sentence) > sentences_up_to:
                    bubbles.append(current)
                    current = ""
                # The split took the space after an English sentence; put it back between two.
                current += (" " if current and current[-1] in ".!?;" else "") + sentence
            if current:
                bubbles.append(current)
        else:
            bubbles.append("\n".join(lines))
    pieces = [piece for bubble in bubbles for piece in split_reply(bubble, limit)]
    while len(pieces) > max_bubbles:
        pairs = [(len(pieces[i]) + len(pieces[i + 1]), i) for i in range(len(pieces) - 1)
                 if len(pieces[i]) + len(pieces[i + 1]) + 1 <= limit]
        if not pairs:
            break
        _, i = min(pairs)
        pieces[i:i + 2] = [pieces[i] + "\n" + pieces[i + 1]]
    return pieces


def spoken_text(text: str) -> str:
    """What a voice reads: Markdown markers and pictographs (emoji) removed, whitespace collapsed."""
    plain = "".join(ch for ch in plain_text(text) if unicodedata.category(ch) not in {"So", "Cs"})
    return " ".join(plain.split())


def voice_note_audio(pcm: bytes, rate: int) -> tuple[bytes, str]:
    """A voice note WeChat plays on a phone with one tap: AAC in M4A where macOS can make it, WAV otherwise.

    iLink accepts a bot's native voice item but does not deliver it (tried 2026-10-18), so speech goes
    out as an audio file."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    audio = buffer.getvalue()
    converter = Path("/usr/bin/afconvert")
    if sys.platform == "darwin" and converter.exists():
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder, "voice.wav"), Path(folder, "voice.m4a")
            source.write_bytes(audio)
            try:
                subprocess.run([str(converter), "-f", "m4af", "-d", "aac", "-b", "48000", str(source), str(target)],
                               check=True, capture_output=True, timeout=60)
                return target.read_bytes(), "m4a"
            except (OSError, subprocess.SubprocessError) as exc:
                logger.warning("voice note stays WAV: %s", type(exc).__name__)
    return audio, "wav"


def typing_pause(text: str) -> float:
    """Seconds between two messages, about the time it takes to type the next one."""
    return min(2.5, 0.5 + len(text) / 25)


class WeixinAdapter(ChannelAdapter):
    name = "weixin"

    def __init__(self, config: WeixinConfig, handler: MessageHandler, *, store: WeixinStore | None = None,
                 client: IlinkClient | None = None, keep_awake: KeepAwake | None = None,
                 owner_retry: float = 30.0, failure_delays: tuple[float, float] = (2.0, 30.0),
                 pause: Callable[[str], float] = typing_pause) -> None:
        super().__init__(handler)
        self.config = config
        self.store = store or WeixinStore()
        self._client = client
        self._keep_awake = keep_awake if keep_awake is not None else KeepAwake("Astra WeChat channel online")
        self._owner_retry = owner_retry
        self._failure_delays = failure_delays
        self._task: asyncio.Task | None = None
        self._lock: InstanceLock | None = None
        self._state = "stopped"
        self._error = ""
        self._account: WeixinAccount | None = None
        self._tokens: dict[str, str] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._inbox: asyncio.Queue[dict] = asyncio.Queue()
        self._pause = pause
        self._tickets: dict[str, tuple[str, float]] = {}

    @property
    def running(self) -> bool:
        return self._state == "online"

    def owner(self) -> str:
        """The WeChat user who scanned the login code."""
        account = self._account or self.store.account()
        return account.user_id if account is not None else ""

    def state_key(self) -> tuple:
        """What a status shows that can change on its own, read without touching the disk."""
        return (self._state, self._lock is not None, self._keep_awake.active, self._error)

    def status(self) -> dict[str, Any]:
        account = self.store.account()
        return {"state": self._state, "account": mask(account.account_id) if account else "",
                "owner": self._lock is not None, "keep_awake": self._keep_awake.active, "error": self._error}

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._error = ""
        self._task = asyncio.create_task(self._run(), name="weixin-channel")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and not task.done():
            await _finish(task, 10.0)
        self._release()
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._state != "expired":
            self._state = "stopped"

    def _release(self) -> None:
        self._keep_awake.stop()
        lock, self._lock = self._lock, None
        if lock is not None:
            lock.release()

    async def _run(self) -> None:
        while True:
            account = self.store.account()
            if account is None:
                self._state = "logged_out"
                return
            lock = InstanceLock(self.store.owner_lock)
            try:
                lock.acquire()
            except InstanceAlreadyRunning:
                # Another backend polls this login; take over when it exits.
                self._state = "standby"
                await asyncio.sleep(self._owner_retry)
                continue
            self._lock = lock
            try:
                if self.config.keep_awake:
                    self._keep_awake.start()
                if self._client is None:
                    self._client = IlinkClient()
                self._account = account
                self._tokens = self.store.context_tokens()
                self._state = "online"
                expired = await self._poll(account)
            finally:
                self._release()
            if expired:
                self._state = "expired"
                self._error = "微信登录已失效，请重新扫码登录。"
                return

    async def _poll(self, account: WeixinAccount) -> bool:
        """Deliver messages until cancelled; True when WeChat reports the login expired."""
        assert self._client is not None
        worker = asyncio.create_task(self._work(), name="weixin-inbox")
        cursor, timeout_ms, failures = self.store.sync_buf(), _LONG_POLL_MS, 0
        try:
            while True:
                try:
                    response = await self._client.get_updates(account, cursor, timeout_ms)
                except (httpx.HTTPError, WeixinError) as exc:
                    if _stop_requested():
                        raise asyncio.CancelledError from exc
                    failures += 1
                    self._error = f"连接微信失败：{type(exc).__name__}"
                    logger.warning("weixin poll failed (%d): %s", failures, exc)
                    await asyncio.sleep(self._failure_delays[failures >= 3])
                    continue
                code = response.get("errcode") or response.get("ret") or 0
                if code == _SESSION_EXPIRED:
                    logger.warning("weixin login expired; scan the code again")
                    return True
                if code:
                    failures += 1
                    logger.warning("weixin poll answered ret=%s errcode=%s", response.get("ret"), response.get("errcode"))
                    await asyncio.sleep(self._failure_delays[failures >= 3])
                    continue
                failures, self._error = 0, ""
                suggested = response.get("longpolling_timeout_ms")
                if isinstance(suggested, int) and suggested > 0:
                    timeout_ms = suggested
                for message in response.get("msgs") or []:
                    if isinstance(message, dict):
                        self._inbox.put_nowait(message)
                # The cursor moves only after the batch is queued, so a crash re-delivers rather than drops.
                new_cursor = str(response.get("get_updates_buf") or "")
                if new_cursor and new_cursor != cursor:
                    cursor = new_cursor
                    await asyncio.to_thread(self.store.save_sync_buf, cursor)
        finally:
            await _finish(worker, 5.0)

    async def _work(self) -> None:
        # One message at a time, in the order WeChat delivered them.
        while True:
            message = await self._inbox.get()
            try:
                await self._handle(message)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("weixin message handling failed")
            if _stop_requested():
                raise asyncio.CancelledError

    def _seen_before(self, message_id: str) -> bool:
        if not message_id:
            return False
        if message_id in self._seen:
            return True
        self._seen[message_id] = None
        while len(self._seen) > _SEEN_LIMIT:
            self._seen.popitem(last=False)
        return False

    async def _handle(self, message: dict) -> None:
        account = self._account
        if account is None:
            return
        sender = str(message.get("from_user_id") or "").strip()
        if not sender or sender == account.account_id or self._seen_before(str(message.get("message_id") or "")):
            return
        if message.get("room_id") or message.get("chat_room_id"):
            logger.info("weixin group message ignored")
            return
        allowed = self.config.allow_users or ((account.user_id,) if account.user_id else ())
        if sender not in allowed:
            logger.info("weixin message from a sender who is not allowed: %s", mask(sender))
            return
        token = str(message.get("context_token") or "").strip()
        if token and self._tokens.get(sender) != token:
            self._tokens[sender] = token
            await asyncio.to_thread(self.store.save_context_tokens, dict(self._tokens))
        text, images = await self._content(message.get("item_list") or [])
        text = text.strip()[:self.config.max_input_chars]
        if not text and not images:
            return
        event = ChannelMessage(channel=self.name, conversation_id=sender, sender_id=sender,
                               text=text or "[图片]", message_id=str(message.get("message_id") or ""),
                               images=images, raw=message)

        async def reply(answer: str) -> None:
            await self.send_text(sender, answer)

        # "Typing…" shows in WeChat from the message until the last reply is sent; it is on before
        # the turn starts, so even a quick reply never arrives ahead of it.
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._typing(sender, _TYPING_START), timeout=3.0)
        typing = asyncio.create_task(self._keep_typing(sender), name="weixin-typing")
        try:
            try:
                response = await self.handler(event, reply)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("weixin turn failed")
                response = "Astra 处理消息时发生错误，请稍后重试。"
            if response:
                await reply(response)
        finally:
            await _finish(typing, 5.0)

    async def _content(self, items: list) -> tuple[str, tuple[ChannelImage, ...]]:
        parts: list[str] = []
        images: list[ChannelImage] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == _ITEM_TEXT:
                text = str((item.get("text_item") or {}).get("text") or "")
                quoted = (item.get("ref_msg") or {})
                quoted_text = str((((quoted.get("message_item") or {}).get("text_item")) or {}).get("text") or "")
                if quoted.get("title") or quoted_text:
                    parts.append(f"[引用: {' | '.join(p for p in (str(quoted.get('title') or ''), quoted_text) if p)}]")
                parts.append(text)
            elif kind == _ITEM_VOICE:
                spoken = str((item.get("voice_item") or {}).get("text") or "")
                parts.append(f"[语音，微信转写] {spoken}" if spoken else "[语音]")
            elif kind == _ITEM_IMAGE:
                if len(images) < self.config.max_images:
                    image = await self._image(item.get("image_item") or {})
                    if image is not None:
                        images.append(image)
                    else:
                        parts.append("[图片：没能读取]")
            elif kind == _ITEM_FILE:
                parts.append(f"[文件: {(item.get('file_item') or {}).get('file_name') or '未命名'}]")
            elif kind == _ITEM_VIDEO:
                parts.append("[视频]")
        return "\n".join(part for part in parts if part), tuple(images)

    async def _image(self, payload: dict) -> ChannelImage | None:
        assert self._client is not None
        media = payload.get("media") or {}
        param, full_url = media.get("encrypt_query_param"), media.get("full_url")
        if param:
            url = f"{self._client.cdn_url}/download?encrypted_query_param={quote(str(param), safe='')}"
        elif full_url and urlparse(str(full_url)).scheme == "https" and urlparse(str(full_url)).hostname in _CDN_HOSTS:
            url = str(full_url)
        else:
            return None
        key = media.get("aes_key")
        if payload.get("aeskey"):
            key = base64.b64encode(bytes.fromhex(str(payload["aeskey"]))).decode("ascii")
        try:
            data = await self._client.download(url, self.config.max_image_bytes)
            if key:
                data = await asyncio.to_thread(_decrypt, data, _aes_key(str(key)))
        except (httpx.HTTPError, WeixinError, ValueError, ImportError) as exc:
            logger.warning("weixin image could not be read: %s", type(exc).__name__)
            return None
        kind = _image_type(data)
        return ChannelImage(data_url=f"data:{kind};base64,{base64.b64encode(data).decode('ascii')}", name="weixin-image")

    async def _ticket(self, to: str) -> str:
        account, client = self._account, self._client
        if account is None or client is None:
            return ""
        cached = self._tickets.get(to)
        loop = asyncio.get_running_loop()
        if cached and loop.time() - cached[1] < _TYPING_TICKET_SECONDS:
            return cached[0]
        ticket = await client.typing_ticket(account, to, self._tokens.get(to, ""))
        if ticket:
            self._tickets[to] = (ticket, loop.time())
        return ticket

    async def _typing(self, to: str, status: int) -> None:
        account, client = self._account, self._client
        try:
            ticket = await self._ticket(to)
            if ticket and account is not None and client is not None:
                await client.send_typing(account, to, ticket, status)
        except (httpx.HTTPError, WeixinError) as exc:
            logger.debug("weixin typing indicator failed: %s", type(exc).__name__)

    async def _keep_typing(self, to: str) -> None:
        try:
            while not _stop_requested():
                await asyncio.sleep(_TYPING_REFRESH_SECONDS)
                await self._typing(to, _TYPING_START)
        finally:
            # Without an explicit stop WeChat can keep showing "typing…".
            await asyncio.shield(self._typing(to, _TYPING_STOP))

    async def send_text(self, to: str, text: str) -> None:
        account, client = self._account, self._client
        if account is None or client is None:
            raise WeixinError("the WeChat channel is not online")
        limit = self.config.max_output_chars
        pieces = (chat_bubbles(text, limit, self.config.max_bubbles) if self.config.chat_bubbles
                  else split_reply(plain_text(text), limit))
        for index, piece in enumerate(pieces):
            if index:
                await asyncio.sleep(self._pause(piece))
            await self._send_piece(client, account, to, piece)

    async def send_file(self, message: ChannelMessage, path: str, name: str) -> dict[str, Any]:
        """Send a local file to the person in ``message``: an image or a video as one, anything else as a file."""
        data = await asyncio.to_thread(Path(path).read_bytes)
        suffix = Path(name or path).suffix.lower()
        media = _MEDIA_IMAGE if suffix in _IMAGE_SUFFIXES else _MEDIA_VIDEO if suffix in _VIDEO_SUFFIXES else _MEDIA_FILE
        return await self._send_media(message.conversation_id, data, name or Path(path).name, media)

    async def send_voice_note(self, message: ChannelMessage, pcm: bytes, rate: int) -> dict[str, Any]:
        """Send speech as an audio file the phone plays with a tap."""
        audio, kind = await asyncio.to_thread(voice_note_audio, pcm, rate)
        result = await self._send_media(message.conversation_id, audio, f"语音 {time.strftime('%H:%M')}.{kind}", _MEDIA_FILE)
        return {**result, "kind": "voice", "seconds": round(len(pcm) / 2 / rate, 1)}

    async def _send_media(self, to: str, data: bytes, name: str, media: int) -> dict[str, Any]:
        account, client = self._account, self._client
        if account is None or client is None:
            raise WeixinError("the WeChat channel is not online")
        key, filekey, md5 = secrets.token_bytes(16), secrets.token_hex(16), hashlib.md5(data).hexdigest()
        encrypted = await asyncio.to_thread(_encrypt, data, key)
        url = await client.upload_url(account, to, media_type=media, filekey=filekey, size=len(data),
                                      md5=md5, encrypted_size=len(encrypted), aes_key_hex=key.hex())
        reference = await client.upload(url, encrypted)
        # iLink takes the key as base64 of its hex text; raw-key base64 shows as a grey box.
        source = {"encrypt_query_param": reference, "aes_key": base64.b64encode(key.hex().encode()).decode(), "encrypt_type": 1}
        if media == _MEDIA_IMAGE:
            item = {"type": _ITEM_IMAGE, "image_item": {"media": source, "mid_size": len(encrypted)}}
        elif media == _MEDIA_VIDEO:
            item = {"type": _ITEM_VIDEO, "video_item": {"media": source, "video_size": len(encrypted), "play_length": 0,
                                                        "video_md5": md5}}
        else:
            item = {"type": _ITEM_FILE, "file_item": {"media": source, "file_name": name, "len": str(len(data))}}
        try:
            await self._send_items(client, account, to, [item])
        except httpx.TimeoutException as exc:
            # Uploaded and handed over, but WeChat never confirmed the message.
            raise TimeoutError("WeChat did not confirm the file message") from exc
        return {"status": "sent", "kind": {_MEDIA_IMAGE: "image", _MEDIA_VIDEO: "video"}.get(media, "file")}

    async def _send_piece(self, client: IlinkClient, account: WeixinAccount, to: str, piece: str) -> None:
        await self._send_items(client, account, to, [{"type": _ITEM_TEXT, "text_item": {"text": piece}}])

    async def _send_items(self, client: IlinkClient, account: WeixinAccount, to: str, items: list[dict]) -> None:
        token = self._tokens.get(to, "")
        for attempt in range(_SEND_ATTEMPTS):
            response = await client.send_items(account, to, items, token)
            code = response.get("errcode") or response.get("ret") or 0
            if not code:
                return
            note = str(response.get("errmsg") or response.get("msg") or "").lower()
            if code == _SESSION_EXPIRED and token:
                # A stale reply token: WeChat still takes the message without it.
                token = ""
                self._tokens.pop(to, None)
                continue
            if code == _RATE_LIMITED and note not in {"unknown error", "prepare failed"}:
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            if code == _RATE_LIMITED:
                raise WeixinError("WeChat will not take a reply until this person writes to the bot again")
            raise WeixinError(f"WeChat refused the reply (ret={response.get('ret')}, errcode={response.get('errcode')})")
        raise WeixinError("WeChat kept rate-limiting the reply")
