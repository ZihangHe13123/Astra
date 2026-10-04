"""Shared, explicit guidance application for every local frontend."""

from typing import Any
from collections.abc import Iterable
import shlex
import asyncio


GUIDANCE_USAGE = "Usage: /guidance [status] | /guidance refresh --now"


async def settle_guidance_boundary(task: asyncio.Task | None) -> bool:
    """Bound the final save/cleanup after a frontend has already seen done."""
    if task is None or task.done():
        return True
    done, _ = await asyncio.wait({task}, timeout=1.0)
    return bool(done)


async def guidance_command_busy(task: asyncio.Task | None, reply_done: bool,
                                reviews: Iterable[asyncio.Task] = (), *, apply_now: bool = False) -> bool:
    if any(not review.done() for review in reviews):
        return True
    if task is None or task.done():
        return False
    if not reply_done:
        return True
    return apply_now and not await settle_guidance_boundary(task)


def format_guidance_status(status: dict) -> str:
    if not status["enabled"]:
        return status["message"]
    applied = status["applied"]
    pending = [name for name, changed in status["pending"].items() if changed]
    lines = [f"Applied to this conversation: {len(applied['project_sources'])} project instruction files, "
             f"{len(applied['skills'])} skills."]
    if applied["project_sources"]:
        lines.append("Project files: " + ", ".join(applied["project_sources"]))
    lines.append("Saved changes pending: " + (", ".join(pending) if pending else "none"))
    lines.extend(status["warnings"])
    lines.append(status["message"])
    return "\n".join(lines)


def execute_guidance_command(agent: Any, args: list[str], *, busy: bool = False) -> tuple[str, str]:
    if not args or args == ["status"]:
        return format_guidance_status(agent.guidance_status()), ""
    if args != ["refresh", "--now"]:
        return "", GUIDANCE_USAGE
    if busy:
        return "", "Finish or cancel the active task before applying guidance to this conversation."
    status = agent.refresh_session_guidance()
    if status["enabled"]:
        agent.context.save(allow_empty=True)
        return "Applied saved guidance to this conversation. The model request prefix may refresh.\n" + format_guidance_status(status), ""
    return status["message"], ""


def execute_guidance_or_skill_command(agent: Any, store: Any, command: str, *, busy: bool = False) -> tuple[str, str, str]:
    """Keep frontend routing and parse failures outside the backend's big loop."""
    name = command.split(maxsplit=1)[0].lstrip("/")
    try:
        args = shlex.split(command)[1:]
    except ValueError as exc:
        return name, "", f"Invalid {name} command: {exc}"
    if name == "guidance":
        output, error = execute_guidance_command(agent, args, busy=busy)
    else:
        from .skill_commands import execute_skill_command
        output, error = execute_skill_command(store, args, agent=agent, busy=busy)
    return name, output, error
