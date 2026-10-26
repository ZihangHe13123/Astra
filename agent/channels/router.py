"""Route channel messages through the main Astra agent without context leaks."""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from typing import Any
from pathlib import Path

from agent.core.msg import ContentBlock, Msg
from agent.runtime.tools.grants import enter_grant_space, leave_grant_space
from agent.runtime.usage import reset_usage_context, set_usage_context
from agent.runtime.prompt_budget import prompt_token_budget
from agent.runtime.prompts import channel_prompt_profile
from agent.runtime.context import AgentContext
from agent.runtime.context_compressor import ContextCompressor
from agent.runtime.json_preferences import update_preferences
from agent.runtime.jobs.store import note_interaction
from agent.runtime.paths import state_dir, state_path

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


# A message that is nothing but one of these starts a new conversation with that chat.
NEW_CONVERSATION_COMMANDS = frozenset({"/new", "/reset"})
NEW_CONVERSATION_REPLY = "已开始新的对话，之前的聊天记录都保存着。"


def channel_conversations_path() -> Path:
    """Which conversation each chat is in now, when it started a new one with /new."""
    return state_path("channels", "conversations.json")


def channel_session_name(message: ChannelMessage) -> str:
    kind = "group" if message.is_group else "private"
    identity = f"{message.channel}:{kind}:{message.conversation_id}"
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return f"channel_{message.channel}_{kind}_{digest}"


class AgentChannelRouter:
    """Serialize use of a stateful ReActAgent and park one context per chat."""

    def __init__(self, agent, turn_lock: asyncio.Lock, session_root: Path,
                 *, admission: Callable[[], bool] | None = None,
                 model_for: Callable[[str], Any] | None = None, tasks: Any = None):
        self.agent = agent
        self.turn_lock = turn_lock
        self.session_root = Path(session_root)
        self.admission = admission or (lambda: True)
        # A channel's own model client, or None to answer with the agent's.
        self.model_for = model_for or (lambda channel: None)
        # The task store, when there is one: each chat turn is recorded there as a run with
        # its steps, like a desktop turn, so the activity page shows what Astra did in chats.
        self.tasks = tasks
        self._contexts: dict[str, AgentContext] = {}
        self._template = agent.context

    @staticmethod
    def _current_sessions() -> dict:
        try:
            data = json.loads(channel_conversations_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _session_name(self, message: ChannelMessage) -> str:
        """The chat's current conversation: the first one, or the last started with /new."""
        base = channel_session_name(message)
        current = self._current_sessions().get(base)
        return current if isinstance(current, str) and current.startswith(f"{base}_") else base

    async def _start_new_conversation(self, message: ChannelMessage) -> str:
        """Close the chat's conversation (kept on disk) and send its next message to a new one."""
        base = channel_session_name(message)
        old = self._session_name(message)
        context = self._contexts.pop(old, None)
        if context is not None:
            await context.save_async()
            context.record_session_end("new_conversation")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        name, n = f"{base}_{stamp}", 1
        while name == old or any(self.session_root.glob(f"{name}.*")):
            n += 1
            name = f"{base}_{stamp}_{n}"
        await asyncio.to_thread(update_preferences, channel_conversations_path(),
                                lambda data: data.__setitem__(base, name))
        logger.info("channel %s started a new conversation", message.channel)
        return NEW_CONVERSATION_REPLY

    async def _start_task(self, message: ChannelMessage, context: AgentContext) -> str:
        if self.tasks is None:
            return ""
        try:
            run = await asyncio.to_thread(
                self.tasks.start_run, f"channel:{message.channel}:{uuid.uuid4().hex}", message.text[:8000],
                session_id=context.session_scope, model=getattr(getattr(self.agent.llm, "config", None), "model", ""),
                source=message.channel)
        except Exception:
            logger.exception("channel turn not recorded in the task store")
            return ""
        return str(run.get("id") or "")

    async def _finish_task(self, task_id: str, outcome: str) -> None:
        try:
            await asyncio.to_thread(self.tasks.finish_run, task_id, outcome)
        except Exception:
            logger.exception("channel turn outcome not recorded in the task store")

    async def record_outbound(self, channel: str, conversation_id: str, text: str) -> None:
        """Keep a message Astra sent on its own, such as a job result, in that private chat's
        conversation, so the person's reply ("tell me more about the second one") has it."""
        message = ChannelMessage(channel=channel, conversation_id=conversation_id, sender_id=conversation_id, text="")
        async with self.turn_lock:
            context = self._context(self._session_name(message), channel)
            context.add_assistant(text)
            await context.save_async()

    def _budget(self, llm) -> int:
        """Compact a channel conversation for the model answering it: its own, or the window's."""
        config = getattr(llm, "config", None)
        limit = getattr(config, "context_limit", None)
        if config is None or not limit:
            return self._template.max_prompt_tokens
        return prompt_token_budget(limit, config.max_tokens, model=config.model, provider=config.provider,
                                   auto_compact_token_limit=config.auto_compact_token_limit)

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
        self._use_channel_persona(context, channel)
        self._contexts[session_name] = context
        return context

    def _use_channel_persona(self, context: AgentContext, channel: str) -> None:
        """A channel's own version of the selected persona, when there is one (WeChat: tone, no actions).

        A conversation still on another persona switches when it is opened: its system prompt is
        rebuilt once and later turns keep that prefix. One already on the version is left as it is.
        """
        template = self._template
        profile = channel_prompt_profile(template.persona_id, channel)
        if profile is None or (context.persona_id, context.persona_definition_version) == (profile.name, profile.version):
            return
        state = replace(profile.default_state(), relationship_context=template.persona_relationship_context,
                        affect=template.persona_affect)
        context.set_persona(state, profile.system_prompt(state))

    async def handle(
        self,
        message: ChannelMessage,
        reply_sink: ReplySink | None = None,
    ) -> str:
        if not self.admission():
            return "Astra is preparing a restart. Please retry after it reconnects."
        if not message.images and message.text.strip().lower() in NEW_CONVERSATION_COMMANDS:
            async with self.turn_lock:
                return await self._start_new_conversation(message)
        session_name = self._session_name(message)
        note_interaction(state_dir())
        async with self.turn_lock:
            if not self.admission():
                return "Astra is preparing a restart. Please retry after it reconnects."
            previous = self.agent.context
            previous_llm = self.agent.llm
            context = self._context(session_name, message.channel)
            self.agent.context = context
            llm = self.model_for(message.channel)
            if llm is not None:
                self.agent.llm = llm
            context.max_prompt_tokens = self._budget(llm)
            if context.compressor is not None:
                context.compressor.llm = self.agent.llm
            token = active_channel.set(message.channel)
            # This chat's own approvals: the desktop's grants never cover it, nor another chat's.
            space_token = enter_grant_space(f"channel:{session_name}")
            usage_tokens = set_usage_context(source="channel", session=session_name)
            message_token = active_channel_message.set(message)
            media: list[str] = []
            media_token = channel_turn_media.set(media)
            task_id, outcome = "", "failed"
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
                task_id = await self._start_task(message, context)
                if task_id:
                    msg.metadata["task_id"] = task_id
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
                outcome = "completed"
                # A turn that answered with a voice note or a file alone has said what it had to.
                response = "".join(chunks).strip() or ("" if media else "(no response)")
                return "" if reply_sink is not None and chunks else response
            except asyncio.CancelledError:
                outcome = "cancelled"
                raise
            finally:
                if task_id:
                    await self._finish_task(task_id, outcome)
                channel_turn_media.reset(media_token)
                active_channel_message.reset(message_token)
                reset_usage_context(usage_tokens)
                leave_grant_space(space_token)
                active_channel.reset(token)
                self.agent.context = previous
                self.agent.llm = previous_llm
