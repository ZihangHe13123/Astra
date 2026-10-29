"""OneBot v11 reverse-WebSocket adapter for NapCat.

NapCat remains the QQ protocol client. This adapter replaces AstrBot: NapCat
connects directly to the Astra backend process and messages go straight into
the ReAct agent router.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from typing import Any
from urllib.parse import parse_qs, urlparse

from websockets.asyncio.server import ServerConnection, serve

from .base import ChannelAdapter, ChannelImage, ChannelMessage, MessageHandler
from .config import OneBotConfig
from .media import download_image_data_url


logger = logging.getLogger(__name__)
_CQ_AT_RE = re.compile(r"\[CQ:at,qq=([^,\]]+)[^\]]*\]")


def _text_from_segments(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return str(value or "")
    chunks: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or "")
        raw_data = item.get("data")
        data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
        if kind == "text":
            chunks.append(str(data.get("text") or ""))
        elif kind == "at":
            chunks.append(f"[CQ:at,qq={data.get('qq', '')}]")
        elif kind in {"image", "file", "record", "video"}:
            chunks.append(f"[{kind}]")
    return "".join(chunks)


def _strip_self_mention(text: str, self_id: str) -> tuple[str, bool]:
    mentioned = False

    def replace(match: re.Match[str]) -> str:
        nonlocal mentioned
        if match.group(1) in {self_id, "all"}:
            mentioned = match.group(1) == self_id
            return " "
        return match.group(0)

    return _CQ_AT_RE.sub(replace, text).strip(), mentioned


def _strip_prefix(text: str, prefixes: tuple[str, ...]) -> tuple[str, bool]:
    stripped = text.lstrip()
    for prefix in sorted(prefixes, key=len, reverse=True):
        if prefix and stripped.startswith(prefix):
            return stripped[len(prefix):].lstrip(), True
    return stripped, False


def _chunks(text: str, limit: int) -> list[str]:
    text = text.strip()
    if not text:
        return []
    return [text[index:index + limit] for index in range(0, len(text), limit)]


class OneBotAdapter(ChannelAdapter):
    name = "qq"

    def __init__(self, config: OneBotConfig, handler: MessageHandler):
        super().__init__(handler)
        self.config = config
        self._server = None
        self._connections: set[ServerConnection] = set()
        self._send_locks: dict[int, asyncio.Lock] = {}
        self._pending_actions: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._pending_connections: dict[str, ServerConnection] = {}

    @property
    def running(self) -> bool:
        return self._server is not None

    async def start(self) -> None:
        if self._server is not None:
            return
        if not self.config.allow_users and not self.config.owner_users:
            logger.warning("QQ has no admitted users; configure allow_users for chat or owner_users for private local control")
        self._server = await serve(
            self._connection,
            self.config.host,
            self.config.port,
            ping_interval=20,
            ping_timeout=20,
            max_size=2 * 1024 * 1024,
        )

    async def stop(self) -> None:
        server, self._server = self._server, None
        for connection in list(self._connections):
            await connection.close(code=1001, reason="Astra channel shutdown")
        self._connections.clear()
        self._send_locks.clear()
        for future in self._pending_actions.values():
            if not future.done():
                future.set_exception(ConnectionError("OneBot channel stopped"))
        self._pending_actions.clear()
        if server is not None:
            server.close()
            await server.wait_closed()

    def _authorized(self, connection: ServerConnection) -> bool:
        expected = self.config.access_token
        if not expected:
            return True
        request = getattr(connection, "request", None)
        headers = getattr(request, "headers", {})
        authorization = str(headers.get("Authorization", ""))
        if authorization.lower().startswith("bearer "):
            return authorization[7:].strip() == expected
        path = str(getattr(request, "path", "") or "")
        supplied = parse_qs(urlparse(path).query).get("access_token", [""])[0]
        return supplied == expected

    async def _connection(self, connection: ServerConnection) -> None:
        if not self._authorized(connection):
            await connection.close(code=1008, reason="Invalid OneBot access token")
            return
        self._connections.add(connection)
        self._send_locks[id(connection)] = asyncio.Lock()
        logger.info("OneBot client connected remote=%s", connection.remote_address)
        try:
            async for raw in connection:
                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, TypeError):
                    logger.warning("discarding invalid OneBot payload")
                    continue
                if isinstance(payload, dict):
                    echo = str(payload.get("echo") or "")
                    pending = (self._pending_actions.get(echo)
                               if self._pending_connections.get(echo) is connection else None)
                    # Missing echoes can match only one unambiguous request on this connection.
                    if (
                        pending is None
                        and not echo
                        and "retcode" in payload
                        and self._pending_actions
                    ):
                        candidates = [future for key, future in self._pending_actions.items()
                                      if self._pending_connections.get(key) is connection and not future.done()]
                        if len(candidates) == 1:
                            pending = candidates[0]
                        elif candidates:
                            for future in candidates:
                                future.set_exception(RuntimeError(
                                    "Ambiguous OneBot receipt: action outcome unknown; do not resend automatically"))
                            continue
                    if pending is not None:
                        if not pending.done():
                            pending.set_result(payload)
                        continue
                    await self._handle_payload(connection, payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("OneBot connection failed")
        finally:
            for key, future in list(self._pending_actions.items()):
                if self._pending_connections.get(key) is connection and not future.done():
                    future.set_exception(RuntimeError("OneBot disconnected: action outcome unknown; do not resend automatically"))
            self._connections.discard(connection)
            self._send_locks.pop(id(connection), None)
            logger.info("OneBot client disconnected")

    async def _handle_payload(self, connection: ServerConnection, payload: dict[str, Any]) -> None:
        if payload.get("post_type") != "message":
            return
        message_type = str(payload.get("message_type") or "")
        is_group = message_type == "group"
        if message_type not in {"group", "private"}:
            return
        if is_group and not self.config.group_enabled:
            return
        if not is_group and not self.config.private_enabled:
            return

        sender_id = str(payload.get("user_id") or "")
        group_id = str(payload.get("group_id") or "")
        self_id = str(payload.get("self_id") or "")
        if not sender_id or sender_id == self_id:
            return
        if sender_id not in {*self.config.allow_users, *self.config.owner_users}:
            return
        if is_group and self.config.allow_groups and group_id not in self.config.allow_groups:
            return

        text = _text_from_segments(payload.get("message", payload.get("raw_message", "")))
        text, mentioned = _strip_self_mention(text, self_id)
        text, prefixed = _strip_prefix(text, self.config.wake_prefixes)
        if is_group and self.config.group_require_mention and not (mentioned or prefixed):
            return
        text = text.strip()[:self.config.max_input_chars]
        if not text:
            return
        images = await self._load_images(payload.get("message"))

        raw_sender = payload.get("sender")
        sender: dict[str, Any] = raw_sender if isinstance(raw_sender, dict) else {}
        event = ChannelMessage(
            channel=self.name,
            conversation_id=group_id if is_group else sender_id,
            sender_id=sender_id,
            text=text,
            is_group=is_group,
            message_id=str(payload.get("message_id") or ""),
            sender_name=str(sender.get("card") or sender.get("nickname") or ""),
            images=images,
            raw=payload,
            local_tools=not is_group and sender_id in self.config.owner_users,
        )
        action = "send_group_msg" if is_group else "send_private_msg"
        target_id = int(group_id if is_group else sender_id)

        async def send_reply(response: str) -> None:
            for chunk in _chunks(response, self.config.max_output_chars):
                params: dict[str, Any] = {"message": chunk}
                params["group_id" if is_group else "user_id"] = target_id
                await self._send(connection, {
                    "action": action,
                    "params": params,
                    "echo": f"astra-{uuid.uuid4().hex[:12]}",
                })

        try:
            response = await self.handler(event, send_reply)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("OneBot message handling failed")
            response = "Astra 处理消息时发生错误，请稍后重试。"

        if response:
            await send_reply(response)

    async def _send(self, connection: ServerConnection, payload: dict[str, Any]) -> None:
        lock = self._send_locks.setdefault(id(connection), asyncio.Lock())
        async with lock:
            await connection.send(json.dumps(payload, ensure_ascii=False))

    async def _load_images(self, value: Any) -> tuple[ChannelImage, ...]:
        if not isinstance(value, list):
            return ()
        images: list[ChannelImage] = []
        for item in value:
            if len(images) >= self.config.max_images:
                break
            if not isinstance(item, dict):
                continue
            if item.get("type") != "image":
                continue
            raw_data = item.get("data")
            data: dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
            url = str(data.get("url") or "").strip()
            if not url:
                continue
            try:
                data_url, _content_type = await download_image_data_url(
                    url,
                    max_bytes=self.config.max_image_bytes,
                )
            except Exception as exc:
                logger.warning("failed to load OneBot image url=%s error=%s", url, exc)
                continue
            images.append(ChannelImage(
                data_url=data_url,
                name=str(data.get("file") or "qq-image"),
                source_url=url,
            ))
        return tuple(images)

    async def send_file(
        self,
        message: ChannelMessage,
        path: str,
        name: str,
    ) -> dict[str, Any]:
        if message.is_group:
            action = "upload_group_file"
            params: dict[str, Any] = {
                "group_id": int(message.conversation_id),
                "file": path,
                "name": name,
            }
        else:
            action = "upload_private_file"
            params = {
                "user_id": int(message.conversation_id),
                "file": path,
                "name": name,
            }
        connection = next(iter(self._connections), None)
        if connection is None:
            raise ConnectionError("NapCat is not connected")
        return await self._call_action(connection, action, params)

    async def _call_action(
        self,
        connection: ServerConnection,
        action: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        echo = f"astra-{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self._pending_actions[echo] = future
        self._pending_connections[echo] = connection
        try:
            await self._send(connection, {
                "action": action,
                "params": params,
                "echo": echo,
            })
            try:
                response = await asyncio.wait_for(future, timeout=30)
            except TimeoutError as exc:
                raise RuntimeError("OneBot response timed out: action outcome unknown; do not resend automatically") from exc
        finally:
            self._pending_actions.pop(echo, None)
            self._pending_connections.pop(echo, None)
        if response.get("status") != "ok" or int(response.get("retcode") or 0) != 0:
            raise RuntimeError(
                str(response.get("message") or response.get("wording") or "OneBot action failed")
            )
        return response
