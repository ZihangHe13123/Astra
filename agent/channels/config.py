"""Configuration for native Astra channels.

The file is intentionally opt-in. Merely upgrading Astra must not bind a
network port or start replying in an existing QQ group.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / ".astra" / "channels.json"
# Problems already logged, so a file read for every message does not repeat them.
_REPORTED: set[str] = set()


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


def _whole(value: Any) -> int | None:
    """A whole number written as a number or as plain digits ("52428800", "-5"); None for anything else."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str) and re.fullmatch(r"[+-]?[0-9]+", value.strip()):
        return int(value.strip())
    return None


def _number(data: dict[str, Any], key: str, default: int, problems: list[str], prefix: str, *,
            low: int, high: int | None = None, zero_is_unset: bool = True, invalid: int | None = None,
            invalid_note: str = "") -> int:
    """A whole-number setting, kept within [low, high]. Only a missing one (absent or null), or 0 where 0
    means unset, takes the default.

    A value that is set but is not a whole number ("50 MB", "--5", "") must not stop the backend: it is
    recorded in ``problems`` and replaced by ``invalid`` (the default unless a safer value is given)."""
    value = data.get(key)
    number = default if value is None else _whole(value)
    if number is None:
        number = default if invalid is None else invalid
        problems.append(f"{prefix}.{key} 应为整数，现在是 {json.dumps(value, ensure_ascii=False)}；"
                        + (invalid_note or f"暂按 {number} 处理。"))
        return number
    if zero_is_unset and number == 0:
        number = default
    return max(low, number if high is None else min(high, number))


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
    owner_users: tuple[str, ...] = ()
    allow_groups: tuple[str, ...] = ()
    max_input_chars: int = 8_000
    max_output_chars: int = 1_800
    max_images: int = 4
    max_image_bytes: int = 10 * 1024 * 1024
    send_files_enabled: bool = False
    file_allow_users: tuple[str, ...] = ()
    send_file_roots: tuple[str, ...] = (str(PROJECT_ROOT),)
    max_send_file_bytes: int = 50 * 1024 * 1024
    # Settings in channels.json that could not be read, each with what is used instead.
    problems: tuple[str, ...] = field(default=(), compare=False)

    @property
    def access_token(self) -> str:
        return os.getenv(self.access_token_env, "").strip()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OneBotConfig":
        problems: list[str] = []

        def number(key: str, default: int, **limits: Any) -> int:
            return _number(data, key, default, problems, "qq", **limits)

        return cls(
            enabled=_bool(data.get("enabled")),
            host=str(data.get("host") or "127.0.0.1"),
            port=number("port", 2280, low=1, high=65_535),
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
            owner_users=_items(data.get("owner_users")),
            allow_groups=_items(data.get("allow_groups")),
            max_input_chars=number("max_input_chars", 8_000, low=1),
            max_output_chars=number("max_output_chars", 1_800, low=100),
            max_images=number("max_images", 4, low=1, high=8),
            max_image_bytes=number("max_image_bytes", 10 * 1024 * 1024, low=1024),
            send_files_enabled=_bool(data.get("send_files_enabled")),
            file_allow_users=_items(data.get("file_allow_users")),
            send_file_roots=_items(
                data.get("send_file_roots"),
                (str(PROJECT_ROOT),),
            ),
            max_send_file_bytes=number("max_send_file_bytes", 50 * 1024 * 1024, low=1024),
            problems=tuple(problems),
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
    # A catalog model key for WeChat turns ("deepseek::deepseek-flash"); empty follows the default model.
    model: str = ""
    send_files_enabled: bool = True
    file_allow_users: tuple[str, ...] = ()
    send_file_roots: tuple[str, ...] = (str(Path.home()),)
    max_send_file_bytes: int = 50 * 1024 * 1024
    # Files and videos the person sends are saved here, one folder a day; empty is ~/Downloads/Astra/微信,
    # and a relative folder is under the home folder.
    receive_files_dir: str = ""
    # 0 leaves received files on WeChat's side: the conversation gets only their names.
    max_receive_file_bytes: int = 50 * 1024 * 1024
    # Settings in channels.json that could not be read, each with what is used instead.
    problems: tuple[str, ...] = field(default=(), compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WeixinConfig":
        problems: list[str] = []

        def number(key: str, default: int, **limits: Any) -> int:
            return _number(data, key, default, problems, "weixin", **limits)

        return cls(
            enabled=_bool(data.get("enabled")),
            allow_users=_items(data.get("allow_users")),
            max_input_chars=number("max_input_chars", 8_000, low=1),
            max_output_chars=number("max_output_chars", 1_800, low=100, high=2_000),
            # 0 is a valid image limit (text only), so only a missing value takes the default.
            max_images=number("max_images", 4, low=0, high=8, zero_is_unset=False),
            max_image_bytes=number("max_image_bytes", 10 * 1024 * 1024, low=1024),
            keep_awake=_bool(data.get("keep_awake"), True),
            chat_bubbles=_bool(data.get("chat_bubbles"), True),
            max_bubbles=number("max_bubbles", 8, low=1, high=20),
            model=str(data.get("model") or "").strip(),
            send_files_enabled=_bool(data.get("send_files_enabled"), True),
            file_allow_users=_items(data.get("file_allow_users")),
            send_file_roots=_items(data.get("send_file_roots"), (str(Path.home()),)),
            max_send_file_bytes=number("max_send_file_bytes", 50 * 1024 * 1024, low=1024),
            receive_files_dir=str(data.get("receive_files_dir") or "").strip(),
            # A limit that cannot be read downloads nothing rather than the default 50 MB.
            max_receive_file_bytes=number(
                "max_receive_file_bytes", 50 * 1024 * 1024, low=0, zero_is_unset=False, invalid=0,
                invalid_note="收到的文件先不下载，只收文件名；写成字节数（如 52428800）后生效。"),
            problems=tuple(problems),
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


def set_channel_setting(section: str, key: str, value: Any, path: str | Path | None = None) -> Path:
    """Change one setting of one channel in the config file, keeping everything else in it.

    A file that is there but is not valid JSON is never written over (it would lose every other setting):
    UnreadablePreferences, an OSError, is raised instead."""
    from agent.runtime.json_preferences import update_preferences

    def change(data: dict) -> None:
        current = data.get(section)
        data[section] = {**(current if isinstance(current, dict) else {}), key: value}

    return update_preferences(channels_config_path(path), change, keep_unreadable=True)


def set_channel_enabled(section: str, enabled: bool, path: str | Path | None = None) -> Path:
    """Switch one channel in the config file, keeping everything else in it."""
    return set_channel_setting(section, "enabled", enabled, path)


def load_channels_config(path: str | Path | None = None, *, strict: bool = False) -> ChannelsConfig:
    """The channels' settings; a missing file switches every channel off.

    A file that cannot be read as a JSON object is also taken as every channel off, unless ``strict``: then
    it raises, so a running channel that follows the file keeps its settings while an editor rewrites it."""
    source = channels_config_path(path)
    data: dict[str, Any] = {}
    if source.exists():
        try:
            loaded = json.loads(source.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict):
                raise ValueError("channels.json does not hold a JSON object")
            data = loaded
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            if strict:
                raise
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
    config = ChannelsConfig(onebot=OneBotConfig.from_dict(onebot_data), weixin=WeixinConfig.from_dict(weixin_data))
    for problem in (*config.onebot.problems, *config.weixin.problems):
        if problem not in _REPORTED:
            _REPORTED.add(problem)
            logger.warning("%s: %s", source, problem)
    return config
