"""Configuration for native Astra channels.

The file is intentionally opt-in. Merely upgrading Astra must not bind a
network port or start replying in an existing QQ group.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / ".astra" / "channels.json"


def _bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _items(
    value: Any,
    default: tuple[str, ...] = (),
    *,
    preserve_whitespace: bool = False,
) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, (list, tuple)):
        if preserve_whitespace:
            return tuple(str(item) for item in value if str(item).strip())
        return tuple(str(item).strip() for item in value if str(item).strip())
    return default


@dataclass(frozen=True)
class OneBotConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 2280
    access_token_env: str = "ASTRA_QQ_ACCESS_TOKEN"
    private_enabled: bool = True
    group_enabled: bool = True
    group_require_mention: bool = True
    wake_prefixes: tuple[str, ...] = ("/",)
    allow_users: tuple[str, ...] = ()
    allow_groups: tuple[str, ...] = ()
    max_input_chars: int = 8_000
    max_output_chars: int = 1_800
    max_images: int = 4
    max_image_bytes: int = 10 * 1024 * 1024
    send_files_enabled: bool = False
    file_allow_users: tuple[str, ...] = ()
    send_file_roots: tuple[str, ...] = (str(PROJECT_ROOT),)
    max_send_file_bytes: int = 50 * 1024 * 1024

    @property
    def access_token(self) -> str:
        return os.getenv(self.access_token_env, "").strip()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OneBotConfig":
        return cls(
            enabled=_bool(data.get("enabled")),
            host=str(data.get("host") or "127.0.0.1"),
            port=max(1, min(65_535, int(data.get("port") or 2280))),
            access_token_env=str(data.get("access_token_env") or "ASTRA_QQ_ACCESS_TOKEN"),
            private_enabled=_bool(data.get("private_enabled"), True),
            group_enabled=_bool(data.get("group_enabled"), True),
            group_require_mention=_bool(data.get("group_require_mention"), True),
            wake_prefixes=_items(
                data.get("wake_prefixes"),
                ("/",),
                preserve_whitespace=True,
            ),
            allow_users=_items(data.get("allow_users")),
            allow_groups=_items(data.get("allow_groups")),
            max_input_chars=max(1, int(data.get("max_input_chars") or 8_000)),
            max_output_chars=max(100, int(data.get("max_output_chars") or 1_800)),
            max_images=max(1, min(8, int(data.get("max_images") or 4))),
            max_image_bytes=max(
                1024,
                int(data.get("max_image_bytes") or 10 * 1024 * 1024),
            ),
            send_files_enabled=_bool(data.get("send_files_enabled")),
            file_allow_users=_items(data.get("file_allow_users")),
            send_file_roots=_items(
                data.get("send_file_roots"),
                (str(PROJECT_ROOT),),
            ),
            max_send_file_bytes=max(
                1024,
                int(data.get("max_send_file_bytes") or 50 * 1024 * 1024),
            ),
        )


@dataclass(frozen=True)
class WeixinConfig:
    """WeChat through Tencent's iLink Bot API (see :mod:`agent.channels.weixin`).

    ``allow_users`` empty means only the WeChat user who scanned the login code.
    """

    enabled: bool = False
    allow_users: tuple[str, ...] = ()
    max_input_chars: int = 8_000
    max_output_chars: int = 1_800
    max_images: int = 4
    max_image_bytes: int = 10 * 1024 * 1024
    keep_awake: bool = True
    # Send a reply as several short messages, the way people text (see weixin.chat_bubbles).
    chat_bubbles: bool = True
    max_bubbles: int = 8
    # channel_send_file: on, to the person who scanned the code (or file_allow_users), from these roots.
    send_files_enabled: bool = True
    file_allow_users: tuple[str, ...] = ()
    send_file_roots: tuple[str, ...] = (str(Path.home()),)
    max_send_file_bytes: int = 50 * 1024 * 1024

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WeixinConfig":
        # 0 is a valid image limit (text only), so only a missing value takes the default.
        images = data.get("max_images")
        return cls(
            enabled=_bool(data.get("enabled")),
            allow_users=_items(data.get("allow_users")),
            max_input_chars=max(1, int(data.get("max_input_chars") or 8_000)),
            max_output_chars=max(100, min(2_000, int(data.get("max_output_chars") or 1_800))),
            max_images=max(0, min(8, int(4 if images is None else images))),
            max_image_bytes=max(1024, int(data.get("max_image_bytes") or 10 * 1024 * 1024)),
            keep_awake=_bool(data.get("keep_awake"), True),
            chat_bubbles=_bool(data.get("chat_bubbles"), True),
            max_bubbles=max(1, min(20, int(data.get("max_bubbles") or 8))),
            send_files_enabled=_bool(data.get("send_files_enabled"), True),
            file_allow_users=_items(data.get("file_allow_users")),
            send_file_roots=_items(data.get("send_file_roots"), (str(Path.home()),)),
            max_send_file_bytes=max(1024, int(data.get("max_send_file_bytes") or 50 * 1024 * 1024)),
        )


@dataclass(frozen=True)
class ChannelsConfig:
    onebot: OneBotConfig = field(default_factory=OneBotConfig)
    weixin: WeixinConfig = field(default_factory=WeixinConfig)

    @property
    def enabled(self) -> bool:
        return self.onebot.enabled or self.weixin.enabled


def channels_config_path(path: str | Path | None = None) -> Path:
    return Path(path or os.getenv("ASTRA_CHANNEL_CONFIG", "") or DEFAULT_CONFIG_PATH)


def set_channel_enabled(section: str, enabled: bool, path: str | Path | None = None) -> Path:
    """Switch one channel in the config file, keeping everything else in it."""
    from agent.runtime.json_preferences import update_preferences

    def change(data: dict) -> None:
        current = data.get(section)
        data[section] = {**(current if isinstance(current, dict) else {}), "enabled": enabled}

    return update_preferences(channels_config_path(path), change)


def load_channels_config(path: str | Path | None = None) -> ChannelsConfig:
    source = channels_config_path(path)
    data: dict[str, Any] = {}
    if source.exists():
        try:
            loaded = json.loads(source.read_text(encoding="utf-8"))
            data = loaded if isinstance(loaded, dict) else {}
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            data = {}

    onebot_data = data.get("qq") or data.get("onebot") or {}
    if not isinstance(onebot_data, dict):
        onebot_data = {}
    if "ASTRA_QQ_ENABLED" in os.environ:
        onebot_data = dict(onebot_data)
        onebot_data["enabled"] = os.environ["ASTRA_QQ_ENABLED"]
    weixin_data = data.get("weixin") or {}
    if not isinstance(weixin_data, dict):
        weixin_data = {}
    return ChannelsConfig(onebot=OneBotConfig.from_dict(onebot_data), weixin=WeixinConfig.from_dict(weixin_data))
