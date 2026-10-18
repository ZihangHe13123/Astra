"""Route channel messages through the main Astra agent without context leaks."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import logging
from collections.abc import Callable
from pathlib import Path

from agent.core.msg import ContentBlock, Msg
from agent.runtime.context import AgentContext
from agent.runtime.context_compressor import ContextCompressor

from .base import ChannelMessage, ReplySink
from .weixin import CHAT_STYLE as WEIXIN_CHAT_STYLE


logger = logging.getLogger(__name__)
active_channel: contextvars.ContextVar[str] = contextvars.ContextVar(
    "astra_active_channel",
    default="",
)
active_channel_message: contextvars.ContextVar[ChannelMessage | None] = (
    contextvars.ContextVar("astra_active_channel_message", default=None)
)
# What a channel turn sent besides text ("file", "voice"). A list the turn's tools append to,
# since a tool may run in a task of its own, where setting a variable would not reach the turn.
channel_turn_media: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar(
    "astra_channel_turn_media", default=None,
)


# How to talk on a channel, added to the system prompt of its conversations only.
CHANNEL_STYLES = {"weixin": WEIXIN_CHAT_STYLE}


def channel_session_name(message: ChannelMessage) -> str:
    kind = "group" if message.is_group else "private"
    identity = f"{message.channel}:{kind}:{message.conversation_id}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"channel_{message.channel}_{kind}_{digest}"


class AgentChannelRouter:
    """Serialize use of a stateful ReActAgent and park one context per chat."""

    def __init__(self, agent, turn_lock: asyncio.Lock, session_root: Path,
                 *, admission: Callable[[], bool] | None = None):
        self.agent = agent
        self.turn_lock = turn_lock
        self.session_root = Path(session_root)
        self.admission = admission or (lambda: True)
        self._contexts: dict[str, AgentContext] = {}
        self._template = agent.context

    def _context(self, session_name: str, channel: str = "") -> AgentContext:
        cached = self._contexts.get(session_name)
        if cached is not None:
            return cached
        template = self._template
        context = AgentContext(
            system_prompt=template.system_prompt,
            max_messages=template.max_messages,
            max_prompt_tokens=template.max_prompt_tokens,
            show_reasoning=False,
        )
        context.persona_id = template.persona_id
        context.persona_definition_version = template.persona_definition_version
        context.persona_state_revision = template.persona_state_revision
        context.persona_active_mode = template.persona_active_mode
        context.persona_relationship_context = template.persona_relationship_context
        context.persona_affect = template.persona_affect
        suffix = getattr(template, "_stable_system_suffix", "")
        style = CHANNEL_STYLES.get(channel, "")
        context.set_stable_system_suffix(f"{suffix}\n\n{style}".strip() if style else suffix)
        context.set_tools_token_cost(getattr(template, "_tools_token_cost", 0))
        context.compressor = ContextCompressor(self.agent.llm)
        context.set_session(str(self.session_root / f"{session_name}.json"))
        context.load()
        self._contexts[session_name] = context
        return context

    async def handle(
        self,
        message: ChannelMessage,
        reply_sink: ReplySink | None = None,
    ) -> str:
        if not self.admission():
            return "Astra is preparing a restart. Please retry after it reconnects."
        session_name = channel_session_name(message)
        async with self.turn_lock:
            if not self.admission():
                return "Astra is preparing a restart. Please retry after it reconnects."
            previous = self.agent.context
            context = self._context(session_name, message.channel)
            self.agent.context = context
            token = active_channel.set(message.channel)
            message_token = active_channel_message.set(message)
            media: list[str] = []
            media_token = channel_turn_media.set(media)
            try:
                content = message.text
                if message.is_group and message.sender_name:
                    content = f"[QQ 群成员 {message.sender_name}]\n{content}"
                content_blocks = [ContentBlock.text(content)]
                content_blocks.extend(
                    ContentBlock.image_url(image.data_url)
                    for image in message.images
                )
                msg = Msg(
                    sender=message.sender_id,
                    role="user",
                    content=content_blocks,
                    metadata={
                        "channel": message.channel,
                        "channel_session": session_name,
                        "channel_message_id": message.message_id,
                    },
                )
                chunks: list[str] = []
                phase_chunks: list[str] = []

                async def flush_phase() -> None:
                    if reply_sink is None or not phase_chunks:
                        return
                    phase = "".join(phase_chunks).strip()
                    phase_chunks.clear()
                    if phase:
                        await reply_sink(phase)

                async for event in self.agent.reply_stream(msg):
                    if event.get("type") == "chunk":
                        chunk = str(event.get("content") or "")
                        chunks.append(chunk)
                        phase_chunks.append(chunk)
                    elif event.get("type") == "tool_calls":
                        # A ReAct tool call closes the current assistant phase.
                        # Send its prose now instead of holding every phase until
                        # the entire tool/task loop completes.
                        await flush_phase()
                    elif event.get("type") == "error" and not chunks:
                        chunk = f"[Error: {event.get('message', 'Request failed')}]"
                        chunks.append(chunk)
                        phase_chunks.append(chunk)
                await flush_phase()
                context.save()
                # A turn that answered with a voice note or a file alone has said what it had to.
                response = "".join(chunks).strip() or ("" if media else "(no response)")
                return "" if reply_sink is not None and chunks else response
            finally:
                channel_turn_media.reset(media_token)
                active_channel_message.reset(message_token)
                active_channel.reset(token)
                self.agent.context = previous
