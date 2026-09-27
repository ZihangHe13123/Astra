"""Live browser ownership; persisted browser rows are only history."""
from __future__ import annotations

import asyncio
import contextvars
import functools
import inspect
import logging
from collections.abc import Callable

from .tool_failure import ToolFailure

logger = logging.getLogger(__name__)


class BrowserLifecycle:
    def __init__(self, manager):
        self.manager = manager
        self.active: dict[str, str] = {}
        self._generation = 0
        self._operation_lock = asyncio.Lock()
        self._release_task: asyncio.Task | None = None
        self._release_pending = False
        self._admission_generation: contextvars.ContextVar[int | None] = contextvars.ContextVar(
            "browser_admission_generation", default=None)

    def before_tool(self, name: str, args: dict, tool_def) -> None:
        if name.startswith("browser_") and name not in {"browser_stop", "browser_status"}:
            # Capture before approval can suspend the call. An implicit tab must
            # not resolve to a new conversation's page after a delayed approval.
            self._admission_generation.set(self._generation)

    @property
    def release_state(self) -> str:
        if not self._release_pending:
            return "active" if self.active else "idle"
        task = self._release_task
        if task is not None and task.done() and (task.cancelled() or task.exception()):
            return "release_failed"
        return "releasing"

    def end_session(self, _session_id: str, _reason: str) -> None:
        # Hooks are synchronous: revoke admission before scheduling any cleanup.
        self._generation += 1
        self.active.clear()
        self._release_pending = True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # The next async entry completes cleanup before admission.
        self._start_release()

    def _start_release(self, *, retry: bool = False) -> asyncio.Task:
        task = self._release_task
        completed = task is not None and task.done() and not task.cancelled() and task.exception() is None
        if task is None or completed or (retry and task.done()):
            self._release_task = task = asyncio.create_task(self._release(), name="browser-session-release")
            task.add_done_callback(self._observe_release)
        return task

    @staticmethod
    def _observe_release(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("Browser release failed; admission remains closed: %s", task.exception())

    async def _release(self) -> None:
        async with self._operation_lock:
            try:
                backend = self.manager.backend
                if backend is not None:
                    release = getattr(backend, "release_session", None)
                    if callable(release):
                        result = release()
                        if not inspect.isawaitable(result):
                            raise TypeError("Browser release must be awaitable")
                        await result
                    else:
                        await backend.close_connection()
            finally:
                # An in-flight open may have populated active after revocation.
                self.active.clear()
        self._release_pending = False

    async def hand_over_if_requested(self, *, busy: bool) -> dict | None:
        """Release control to another Astra window that asked for it, only while this one is idle.

        Same path as /browser stop; there is still no idle timeout or lock stealing
        (specs/browser-session-lifecycle.md).
        """
        if busy or self._release_pending or self._operation_lock.locked():
            return None
        extension = getattr(self.manager.backend, "extension", None)
        transport = getattr(extension, "transport", None)
        read = getattr(transport, "release_request", None)
        request = read() if callable(read) else None
        if not isinstance(request, dict):
            return None
        await self.stop()
        transport.clear_release_request()
        return request

    async def stop(self) -> str:
        self._generation += 1
        self.active.clear()
        self._release_pending = True
        await asyncio.shield(self._start_release(retry=True))
        return "Browser control released. User browser remains open. Open or connect again for fresh handles."

    def wrap(self, fn: Callable):
        @functools.wraps(fn)
        async def invoke(*args, **kwargs):
            admitted = self._admission_generation.get()
            generation = self._generation if admitted is None else admitted
            if self._release_pending:
                try:
                    release = self._start_release()
                    if release.cancelled():
                        raise RuntimeError("Browser cleanup was interrupted")
                    await asyncio.shield(release)
                except Exception:
                    return ToolFailure("browser_release_failed", "Browser cleanup failed; control remains unavailable.",
                                       False, "Use /browser stop to retry cleanup before connecting again.")
            async with self._operation_lock:
                if generation != self._generation or self._release_pending:
                    return ToolFailure("browser_session_ended", "Browser session ended before this call could run.",
                                       False, "Open or connect again and obtain a fresh snapshot.",
                                       details={"dispatch_state": "not_dispatched"})
                return await fn(*args, **kwargs)
        return invoke
