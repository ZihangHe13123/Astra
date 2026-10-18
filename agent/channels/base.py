"""Small, protocol-neutral channel contracts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass(frozen=True)
class ChannelImage:
    data_url: str
    name: str = "qq-image"
    source_url: str = field(default="", compare=False, repr=False)


@dataclass(frozen=True)
class ChannelMessage:
    """One normalized inbound message.

    ``conversation_id`` identifies the private peer or group. It is kept
    separate from ``sender_id`` so group members share group history while
    private conversations remain isolated.
    """

    channel: str
    conversation_id: str
    sender_id: str
    text: str
    is_group: bool = False
    message_id: str = ""
    sender_name: str = ""
    images: tuple[ChannelImage, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)


@dataclass(frozen=True)
class FilePolicy:
    """Whether channel_send_file may send a local file in a channel, to whom, and from where."""

    enabled: bool
    recipients: tuple[str, ...]
    roots: tuple[str, ...]
    max_bytes: int


ReplySink = Callable[[str], Awaitable[None]]
MessageHandler = Callable[[ChannelMessage, ReplySink | None], Awaitable[str]]


class ChannelAdapter(ABC):
    """Lifecycle owned by :class:`ChannelManager`."""

    name: str

    def __init__(self, handler: MessageHandler):
        self.handler = handler

    @abstractmethod
    async def start(self) -> None:
        ...

    @abstractmethod
    async def stop(self) -> None:
        ...

    @property
    @abstractmethod
    def running(self) -> bool:
        ...
