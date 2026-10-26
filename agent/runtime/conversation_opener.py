"""The personal thread asks the desktop to open a conversation in a project, to hand it a task.

Only the desktop opens conversations (each is a backend it starts), so the personal thread's backend
names a folder (a path, or a project's name as the desktop lists it) and the desktop answers with the
new conversation's session, or why it could not (specs/personal-thread.md D6). The task itself then
goes through the local mailbox (agent/runtime/peer_link.py) like any other.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable

OPEN_TIMEOUT_SECONDS = 30.0
# How long the new conversation's backend may take to start and join the mailbox.
OPENED_JOIN_SECONDS = 45.0


class ConversationOpenError(RuntimeError):
    pass


class ConversationOpener:
    """Pairs each request to open a conversation with the desktop's answer to it."""

    def __init__(self, send: Callable[[dict], None], *, timeout: float = OPEN_TIMEOUT_SECONDS) -> None:
        self._send = send
        self._timeout = timeout
        self._pending: dict[str, asyncio.Future[dict]] = {}

    async def request(self, where: str) -> dict:
        """Ask the desktop to open a conversation in ``where``; return its ``session`` and ``workspace``."""
        where = str(where or "").strip()
        if not where or len(where) > 1000:
            raise ConversationOpenError("Name the project folder: a path, or a project's name as the desktop lists it.")
        request_id = uuid.uuid4().hex
        future: asyncio.Future[dict] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            self._send({"type": "open_conversation_request", "request_id": request_id, "where": where})
            return await asyncio.wait_for(future, self._timeout)
        except asyncio.TimeoutError:
            raise ConversationOpenError("The desktop did not open the conversation in time.") from None
        finally:
            self._pending.pop(request_id, None)

    def resolve(self, command: dict) -> bool:
        """Settle the request the desktop's answer names. False when nothing is waiting for it."""
        future = self._pending.get(str(command.get("request_id") or ""))
        if future is None or future.done():
            return False
        error, session, workspace = command.get("error"), command.get("session"), command.get("workspace")
        if isinstance(error, str) and error.strip():
            future.set_exception(ConversationOpenError(error.strip()[:500]))
        elif isinstance(session, str) and session.strip():
            future.set_result({"session": session.strip(), "workspace": str(workspace or "")})
        else:
            future.set_exception(ConversationOpenError("The desktop's answer named no conversation."))
        return True
