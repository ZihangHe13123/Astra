"""/weixin: log in to WeChat with a QR code, switch the channel on or off and show its state.

Like /voice it is a control command: answered in every mode and during a turn,
with a ``weixin_status`` event rather than a conversation message.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx

from agent.channels.config import load_channels_config, set_channel_enabled, set_channel_setting
from agent.channels.manager import ChannelManager
from agent.channels.weixin import (
    IlinkClient,
    WeixinError,
    WeixinStore,
    mask,
    qr_login,
    qr_svg,
    qr_text,
)

if TYPE_CHECKING:
    from agent.cli.channel_models import ChannelModels
    from agent.cli.model_catalog import CatalogEntry

logger = logging.getLogger(__name__)

USAGE = "用法：/weixin [status|login|logout|on|off|model <模型>|model default]"


def is_weixin_command(command: str) -> bool:
    return command.lower().split(maxsplit=1)[:1] == ["/weixin"]


class WeixinCommands:
    def __init__(self, manager: Callable[[], ChannelManager | None], send: Callable[[dict], None], *,
                 client_factory: Callable[[], IlinkClient] = IlinkClient,
                 models: ChannelModels | None = None) -> None:
        self._manager = manager
        self._send = send
        self._models = models
        self._client_factory = client_factory
        self._login: asyncio.Task | None = None
        self._qr: dict[str, str] | None = None
        self._stage = ""
        self._last_key: tuple | None = None

    def status_event(self, *, message: str = "", error: str = "") -> dict:
        manager = self._manager()
        adapter = manager.weixin() if manager is not None else None
        config = load_channels_config().weixin
        enabled = config.enabled
        store = adapter.store if adapter is not None else WeixinStore()
        account = store.account()
        status = adapter.status() if adapter is not None else {
            "state": "off" if not enabled else "logged_out" if account is None else "stopped",
            "owner": False, "keep_awake": False, "error": "",
        }
        event: dict = {
            "type": "weixin_status", "enabled": enabled, "logged_in": account is not None,
            "account": mask(account.account_id) if account is not None else "",
            "state": status["state"], "owner": status["owner"], "keep_awake": status["keep_awake"],
            "login": self._stage,
            # The model WeChat answers with: a catalog key, or "" for the default model.
            "model": config.model,
            "model_label": self._label(self._resolve(config.model), config.model) if config.model else "",
            "default_model": self._label(self._models.default() if self._models else None, ""),
        }
        if self._qr is not None:
            event["qr"] = dict(self._qr)
        if message:
            event["message"] = message
        problem = error or str(status.get("error") or "")
        if problem:
            event["error"] = problem
        return event

    def send_status(self, **reply: str) -> None:
        manager = self._manager()
        adapter = manager.weixin() if manager is not None else None
        self._last_key = adapter.state_key() if adapter is not None else None
        self._send(self.status_event(**reply))

    def tick(self) -> None:
        """Report a change the channel made on its own: it came online, went to standby or expired."""
        manager = self._manager()
        adapter = manager.weixin() if manager is not None else None
        if adapter is not None and adapter.state_key() != self._last_key:
            self.send_status()

    async def handle(self, command: str) -> None:
        parts = command.split()
        action = parts[1].lower() if len(parts) > 1 else "status"
        try:
            if action == "model" and len(parts) <= 3:
                self._model(parts[2] if len(parts) > 2 else "")
            elif len(parts) > 2:
                self.send_status(error=USAGE)
            elif action == "status":
                self.send_status()
            elif action == "login":
                self._start_login()
            elif action == "logout":
                await self._logout()
            elif action in {"on", "off"}:
                await self._switch(action == "on")
            else:
                self.send_status(error=USAGE)
        except OSError as exc:
            self.send_status(error=f"微信设置没能保存：{exc}")

    def _model(self, key: str) -> None:
        if not key:
            configured = load_channels_config().weixin.model
            default = self._label(self._models.default() if self._models else None, "")
            self.send_status(message=f"微信回复用的模型：{self._label(self._resolve(configured), configured)}" if configured
                             else f"微信回复用的模型：跟随默认{f'（{default}）' if default else ''}")
            return
        if key.lower() == "default":
            set_channel_setting("weixin", "model", "")
            self.send_status(message="微信改为跟随默认模型，下一条消息生效。")
            return
        entry = self._resolve(key)
        if entry is None:
            self.send_status(error=f"没有这个模型：{key}。用 /model 查看可选模型的键（形如 deepseek::deepseek-flash）。")
            return
        # Save the catalog's own key, so a bare model name means one model from then on.
        set_channel_setting("weixin", "model", entry.key)
        self.send_status(message=f"微信改用 {entry.model_id}，下一条消息生效。")

    def _resolve(self, key: str) -> CatalogEntry | None:
        return self._models.resolve(key) if self._models and key else None

    @staticmethod
    def _label(entry: CatalogEntry | None, fallback: str) -> str:
        return entry.model_id if entry else fallback

    def _start_login(self) -> None:
        if self._login is not None and not self._login.done():
            self.send_status(message="正在等待扫码。")
            return
        self._login = asyncio.create_task(self._run_login(), name="weixin-login")

    async def _run_login(self) -> None:
        client = self._client_factory()

        async def show(link: str) -> None:
            self._qr, self._stage = {"svg": qr_svg(link), "text": qr_text(link)}, "scan"
            self.send_status(message="用微信扫描二维码登录 Astra。")

        async def report(text: str) -> None:
            self._stage = "confirm"
            self.send_status(message=text)

        try:
            account = await qr_login(client, WeixinStore(), show=show, report=report)
        except ImportError:
            self._qr, self._stage = None, ""
            self.send_status(error="显示二维码需要 segno：运行 pip install 'segno>=1.6,<2'，或安装 Astra 的 weixin 可选依赖。")
            return
        except (WeixinError, httpx.HTTPError) as exc:
            self._qr, self._stage = None, ""
            self.send_status(error=f"微信登录失败：{exc}")
            return
        finally:
            await client.close()
        self._qr, self._stage = None, ""
        set_channel_enabled("weixin", True)
        await self._restart()
        self.send_status(message=f"微信已登录（{mask(account.account_id)}），渠道已启用。")

    async def _restart(self) -> None:
        manager = self._manager()
        if manager is None:
            return
        config = load_channels_config()
        adapter = manager.new_weixin(config.weixin) if config.weixin.enabled else None
        await manager.replace("weixin", adapter, config)

    async def _logout(self) -> None:
        if self._login is not None and not self._login.done():
            self._login.cancel()
        self._qr, self._stage = None, ""
        manager = self._manager()
        if manager is not None:
            await manager.replace("weixin", None)
        WeixinStore().forget()
        set_channel_enabled("weixin", False)
        await self._restart()
        self.send_status(message="已退出微信登录，渠道已停用。")

    async def _switch(self, on: bool) -> None:
        if on and WeixinStore().account() is None:
            self.send_status(error="还没有登录微信：先运行 /weixin login。")
            return
        set_channel_enabled("weixin", on)
        await self._restart()
        self.send_status(message="微信渠道已启用。" if on else "微信渠道已停用。")
