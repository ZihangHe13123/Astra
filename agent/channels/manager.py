"""Own channel adapter startup and shutdown as one backend lifecycle."""

from __future__ import annotations

import logging

from .approvals import ChatApprover
from .base import ChannelAdapter, ChannelMessage, FilePolicy, MessageHandler
from .config import ChannelsConfig
from .onebot import OneBotAdapter
from .weixin import WeixinAdapter


logger = logging.getLogger(__name__)


def is_address_in_use(exc: OSError) -> bool:
    # Windows asyncio often drops ``winerror`` and only surfaces the Winsock
    # errno (10048 = WSAEADDRINUSE); Unix uses 48/98.
    return getattr(exc, "winerror", None) == 10048 or getattr(exc, "errno", None) in {
        48,
        98,
        10048,
    }


class ChannelManager:
    def __init__(
        self,
        config: ChannelsConfig,
        handler: MessageHandler,
        *,
        adapters: list[ChannelAdapter] | None = None,
    ):
        self.config = config
        self.handler = handler
        self.adapters = adapters if adapters is not None else self._build_adapters()
        self._started: list[ChannelAdapter] = []

    def _build_adapters(self) -> list[ChannelAdapter]:
        adapters: list[ChannelAdapter] = []
        if self.config.onebot.enabled:
            adapters.append(OneBotAdapter(self.config.onebot, self.handler))
        if self.config.weixin.enabled:
            adapters.append(WeixinAdapter(self.config.weixin, self.handler))
        return adapters

    def adapter(self, name: str) -> ChannelAdapter | None:
        return next((item for item in self.adapters if item.name == name), None)

    async def replace(self, name: str, adapter: ChannelAdapter | None, config: ChannelsConfig | None = None) -> None:
        """Stop the named adapter, then start ``adapter`` in its place (None leaves it off)."""
        if config is not None:
            self.config = config
        current = self.adapter(name)
        if current is not None:
            self.adapters.remove(current)
            if current in self._started:
                self._started.remove(current)
            try:
                await current.stop()
            except Exception:
                logger.exception("channel failed to stop cleanly name=%s", name)
        if adapter is not None:
            self.adapters.append(adapter)
            self._started.append(adapter)
            await adapter.start()

    async def start(self) -> None:
        for adapter in self.adapters:
            # Track before starting so a partially initialized adapter is still
            # included in rollback. Adapter.stop() is required to be idempotent.
            self._started.append(adapter)
            try:
                await adapter.start()
            except OSError as exc:
                if is_address_in_use(exc):
                    logger.warning(
                        "channel port already in use; skipping optional channel name=%s",
                        adapter.name,
                    )
                else:
                    logger.exception("channel failed to start name=%s", adapter.name)
                await self.stop()
                raise
            except Exception:
                logger.exception("channel failed to start name=%s", adapter.name)
                await self.stop()
                raise
            logger.info("channel started name=%s", adapter.name)

    async def stop(self) -> None:
        while self._started:
            adapter = self._started.pop()
            try:
                await adapter.stop()
            except Exception:
                logger.exception("channel failed to stop cleanly name=%s", adapter.name)

    def statuses(self) -> list[dict[str, object]]:
        return [
            {"name": adapter.name, "running": adapter.running}
            for adapter in self.adapters
        ]

    def file_policy(self, channel: str) -> FilePolicy | None:
        """What channel_send_file may do in a channel; None where the channel cannot send files."""
        if channel == "qq":
            qq = self.config.onebot
            return FilePolicy(qq.send_files_enabled, qq.file_allow_users or qq.allow_users,
                              qq.send_file_roots, qq.max_send_file_bytes)
        if channel == "weixin":
            wx = self.config.weixin
            adapter = self.weixin()
            owner = adapter.owner() if adapter is not None else ""
            return FilePolicy(wx.send_files_enabled, wx.file_allow_users or ((owner,) if owner else ()),
                              wx.send_file_roots, wx.max_send_file_bytes)
        return None

    def weixin(self) -> "WeixinAdapter | None":
        adapter = self.adapter("weixin")
        return adapter if isinstance(adapter, WeixinAdapter) else None

    async def send_file(
        self,
        message: ChannelMessage,
        path: str,
        name: str,
    ) -> dict[str, object]:
        adapter = next(
            (item for item in self.adapters if item.name == message.channel),
            None,
        )
        sender = getattr(adapter, "send_file", None)
        if sender is None:
            raise RuntimeError(f"channel does not support file sending: {message.channel}")
        return await sender(message, path, name)

    def approver(self, channel: str) -> ChatApprover | None:
        """The adapter that can ask for an approval in its chat, if this channel has one."""
        adapter = next((item for item in self.adapters if item.name == channel), None)
        return adapter if hasattr(adapter, "ask_approval") and hasattr(adapter, "end_approval") else None  # type: ignore[return-value]

    async def send_voice(self, message: ChannelMessage, pcm: bytes, rate: int) -> dict[str, object]:
        adapter = next((item for item in self.adapters if item.name == message.channel), None)
        sender = getattr(adapter, "send_voice_note", None)
        if sender is None:
            raise RuntimeError(f"the {message.channel} channel cannot send voice notes")
        return await sender(message, pcm, rate)
