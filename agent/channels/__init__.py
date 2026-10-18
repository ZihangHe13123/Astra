"""Native messaging channels managed by the Astra backend process."""

from .base import ChannelAdapter, ChannelMessage
from .config import ChannelsConfig, OneBotConfig, WeixinConfig, load_channels_config
from .manager import ChannelManager

__all__ = [
    "ChannelAdapter",
    "ChannelManager",
    "ChannelMessage",
    "ChannelsConfig",
    "OneBotConfig",
    "WeixinConfig",
    "load_channels_config",
]
