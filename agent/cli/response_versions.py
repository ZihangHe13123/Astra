"""Publish alternate answers without replaying tools or replacing a live session."""
from __future__ import annotations

import asyncio
from contextlib import aclosing
from typing import Callable

from agent.runtime.async_io import durable_io
from agent.runtime.conversation_branches import ConversationBranches


_INSTRUCTION = (
    "Produce an alternative final answer to the user's last request using the conversation "
    "and the tool results already recorded below. Those actions have already happened. "
    "Do not call tools, repeat actions, invent new results, or claim that you performed new "
    "work. If more action is needed, explain that limitation in the answer."
)


class ResponseVersions:
    def __init__(self, agent, send: Callable[[dict], None], changed):
        self.agent, self.send, self.changed = agent, send, changed

    @property
    def branches(self):
        return ConversationBranches(self.agent.context.session_path)

    def state(self) -> dict:
        return self.branches.state(branch_id=self.agent.context.branch_id)

    def _identity(self, command: dict) -> dict:
        request_id = command.get("request_id")
        if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
            raise ValueError("Invalid response operation request identity")
        if command.get("branch_id") != self.agent.context.branch_id:
            raise ValueError("Conversation changed; choose the reply again.")
        revision = command.get("revision")
        if type(revision) is not int or revision < 0:
            raise ValueError("Invalid conversation revision")
        return {"branch_id": command["branch_id"], "expected_revision": revision}

    def _adopt(self, candidate) -> None:
        # A branch shares the physical world, but no ephemeral prompt/steering,
        # context-index state or working-memory cache from its former sibling.
        self.agent.reset_conversation()
        self.agent.context.adopt_session(candidate)

    async def select(self, command: dict) -> None:
        identity = self._identity(command)
        branches = self.branches
        group, version = command.get("group_id"), command.get("version_id")
        if not isinstance(group, str) or not isinstance(version, str):
            raise ValueError("Invalid reply version identity")
        target = await durable_io(branches.resolve_version, group, version, **identity)
        # Hydration/validation may fail. Do it before the durable active pointer changes.
        candidate = await durable_io(self.agent.context.stage_session, self.agent.context.session_path,
                                     branch_id=target)
        try:
            await durable_io(branches.select_version, group, version, **identity)
        except (asyncio.CancelledError, Exception):
            if branches.state()["active_branch"] != target:
                raise
        self._adopt(candidate)
        try:
            await self.changed()
        except Exception as exc:
            raise RuntimeError("The reply version was selected, but the view could not refresh. Reopen this conversation.") from exc

    async def regenerate(self, command: dict) -> None:
        identity = self._identity(command)
        branches = self.branches
        prepared: dict = {}
        committed: dict = {}
        adopted = False
        candidate = None
        request_id = command["request_id"]
        reference = command.get("source_ref")
        if not isinstance(reference, dict):
            raise ValueError("Invalid reply source reference")
        content = ""

        def event(status: str, **fields) -> None:
            self.send({"type": "response_regeneration", "request_id": request_id,
                       "source_ref": reference, "status": status, **fields})

        def prepare() -> None:
            prepared.update(branches.prepare_retry(reference, **identity))

        def publish() -> None:
            committed.update(branches.finish_candidate(prepared["candidate_branch_id"], status="completed"))

        try:
            await durable_io(prepare)
            candidate = await durable_io(self.agent.context.stage_session, self.agent.context.session_path,
                                         branch_id=prepared["candidate_branch_id"])
            candidate.set_tools_token_cost(0)
            # Do not invoke ReAct: that would append a second user request and
            # could dispatch a tool even for an answer-only regeneration.
            messages = candidate.get_prompt(include_runtime_context=False)
            if messages and messages[0].get("role") == "system":
                messages[0] = {**messages[0], "content": messages[0]["content"] + "\n\n" + _INSTRUCTION}
            else:
                messages.insert(0, {"role": "system", "content": _INSTRUCTION})
            if candidate.estimate_prompt_tokens() + 256 > candidate.max_prompt_tokens:
                raise ValueError("This reply exceeds the current model's context limit. Choose a shorter conversation or a larger-context model.")
            event("running", content="")
            content, reasoning, usage, provider_state = "", "", None, None
            completed = False
            kwargs = {"messages": messages, "tools": []}
            if self.agent.llm.supports_forced_tool_choice():
                kwargs["tool_choice"] = "none"
            async with aclosing(self.agent.llm.chat_stream(**kwargs)) as stream:
                async for item in stream:
                    kind = item.get("type")
                    if kind == "tool_calls" or item.get("tool_calls"):
                        raise ValueError("Regeneration requested an action. No tool was executed; the original reply is still selected.")
                    if kind == "chunk":
                        delta = item.get("content", "")
                        content += delta
                        event("running", delta=delta)
                    elif kind == "reasoning":
                        reasoning += item.get("content", "")
                    elif kind == "generation_recovery":
                        content = reasoning = ""
                        event("running", content="")
                    elif kind == "done":
                        if item.get("finish_reason") in {"tool_calls", "length"}:
                            raise ValueError("Regeneration did not finish a complete answer.")
                        completed = True
                        content = item.get("content", content)
                        reasoning = item.get("reasoning_content", reasoning)
                        usage, provider_state = item.get("usage"), item.get("_provider_state")
            if not completed or not isinstance(content, str) or not content.strip():
                raise ValueError("Regeneration returned no complete answer. The original reply is still selected.")
            answer = {"role": "assistant", "content": content}
            if reasoning:
                answer["reasoning_content"] = reasoning
            if provider_state:
                answer["_provider_state"] = provider_state
            candidate.add_assistant_raw(answer)
            candidate.add_usage(usage)
            await candidate.save_async()
            # Preflight the persisted head, including all media, before publishing.
            candidate = await durable_io(self.agent.context.stage_session, self.agent.context.session_path,
                                         branch_id=prepared["candidate_branch_id"])
            try:
                await durable_io(publish)
            except (asyncio.CancelledError, Exception):
                # A directory-sync error or cancellation can arrive after the
                # manifest's atomic replace. Observe its authority before
                # deciding whether the candidate can be marked unsuccessful.
                observed = branches.state()
                published = (observed["active_branch"] == prepared["candidate_branch_id"]
                             and any(version["id"] == prepared["version_id"] and version["status"] == "completed"
                                     for group in observed["groups"] for version in group["versions"]))
                if not published:
                    raise
                committed.update(observed)
            self._adopt(candidate)
            adopted = True
            await self.changed()
            event("completed", content=content)
        except (asyncio.CancelledError, Exception) as exc:
            if committed:
                # UI notification failure must not turn a durable success into
                # a failed version or leave the runtime bound to the old head.
                if candidate is not None and not adopted:
                    self._adopt(candidate)
                self.send({"type": "response_versions", **committed})
                event("completed", content=content)
                raise RuntimeError("The new reply was saved, but the view could not refresh. Reopen this conversation.") from exc
            cancelled = isinstance(exc, asyncio.CancelledError)
            unpublished_candidate = getattr(exc, "candidate_branch_id", None)
            if not prepared and unpublished_candidate:
                prepared["candidate_branch_id"] = unpublished_candidate
            if prepared:
                await durable_io(branches.finish_candidate, prepared["candidate_branch_id"],
                                 status="cancelled" if cancelled else "failed")
            self.send({"type": "response_versions", **self.state()})
            event("cancelled" if cancelled else "failed")
            raise
