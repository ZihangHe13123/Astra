"""Scheduled-job results sent to the person who logged WeChat in, and kept in that chat."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from .manager import ChannelManager
from .router import AgentChannelRouter

logger = logging.getLogger(__name__)


def weixin_job_push(manager: ChannelManager, router: AgentChannelRouter) -> Callable[[str], Awaitable[None]]:
    """A sender for job results. WeChat is looked up at each push: it can be logged in after start."""

    async def push(text: str) -> None:
        adapter = manager.adapter("weixin")
        send = getattr(adapter, "send_text", None)
        owner = getattr(adapter, "owner", None)
        if adapter is None or send is None or owner is None:
            # Not given up on a transient state: WeChat is not set up in this Astra at all.
            raise LookupError("WeChat is not set up")
        to = owner()
        if not adapter.running or not to:
            # Tried again later: WeChat may still be logging in or handing over.
            raise ConnectionError("WeChat is not online")
        # One message where it fits, not chat bubbles: each message spends what WeChat lets the bot send.
        await send(to, text, bubbles=False)
        try:
            await router.record_outbound("weixin", to, text)
        except Exception:
            # Sent already; a second send would repeat it. The inbox keeps the result.
            logger.exception("job result sent to WeChat but not kept in the chat's conversation")

    return push
