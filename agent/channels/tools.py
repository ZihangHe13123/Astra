"""Tools that act on the messaging channel for the current agent turn."""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from agent.runtime.tool_failure import ToolFailure
from agent.runtime.tools.registry import ToolDef, ToolRegistry

from .manager import ChannelManager
from .router import active_channel_message


_SENSITIVE_NAMES = {
    ".env",
    "credentials.json",
    "id_ed25519",
    "id_rsa",
}
_SENSITIVE_SUFFIXES = {".key", ".pem", ".pfx", ".p12"}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def register_channel_tools(
    registry: ToolRegistry,
    manager_getter: Callable[[], ChannelManager | None],
) -> None:
    async def channel_send_file(path: str, name: str = "") -> dict[str, object] | ToolFailure:
        message = active_channel_message.get()
        if message is None:
            raise RuntimeError("channel_send_file is only available during a messaging-channel turn")
        manager = manager_getter()
        if manager is None:
            raise RuntimeError("messaging channel manager is not ready")
        config = manager.config.onebot
        if not config.send_files_enabled:
            raise PermissionError("channel file sending is disabled")
        allowed_users = config.file_allow_users or config.allow_users
        if not allowed_users or message.sender_id not in allowed_users:
            raise PermissionError("sender is not allowed to receive local files")

        try:
            source = Path(path).expanduser().resolve(strict=True)
        except FileNotFoundError:
            # The OS error names the first missing directory, not the path that was asked for.
            raise FileNotFoundError(f"file not found: {path}") from None
        if not source.is_file():
            raise ValueError(f"not a file: {source}")
        roots = [Path(item).expanduser().resolve() for item in config.send_file_roots]
        if not roots or not any(_inside(source, root) for root in roots):
            raise PermissionError("file is outside configured send_file_roots")
        lowered_name = source.name.lower()
        if (
            ".git" in {part.lower() for part in source.parts}
            or lowered_name in _SENSITIVE_NAMES
            or lowered_name.startswith(".env.")
            or source.suffix.lower() in _SENSITIVE_SUFFIXES
        ):
            raise PermissionError("refusing to send a potentially sensitive credential file")
        size = source.stat().st_size
        if size > config.max_send_file_bytes:
            raise ValueError(
                f"file is too large: {size} bytes > {config.max_send_file_bytes} bytes"
            )
        upload_name = Path(name).name if name.strip() else source.name
        try:
            await manager.send_file(message, str(source), upload_name)
        except TimeoutError:
            # The upload was handed to the channel and no confirmation came back.
            return ToolFailure(
                code="channel_send_unconfirmed",
                message=(
                    f"The {message.channel} channel did not confirm the upload of {upload_name}; "
                    "the file may or may not have reached the user."
                ),
                retryable=False,
                recovery_hint=(
                    "Do not send it again on your own. Tell the user the upload was not confirmed "
                    "and ask whether the file arrived."
                ),
                partial=True,
            )
        return {
            "status": "sent",
            "channel": message.channel,
            "name": upload_name,
            "bytes": size,
        }

    registry.register(ToolDef(
        name="channel_send_file",
        description=(
            "Send an existing local file to the user in the active QQ/channel conversation. "
            "Works only in a turn that came from a messaging channel. "
            "Use only when that user explicitly asks to receive a file. The path must be inside "
            "configured send_file_roots; credential files are always blocked. "
            "At most 3 calls per turn: a further call is not run."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path of the existing local file to send.",
                },
                "name": {
                    "type": "string",
                    "description": "Optional filename shown to the recipient.",
                    "default": "",
                },
            },
            "required": ["path"],
        },
        fn=channel_send_file,
        risk="write",
        approval="never",
        timeout=45,
        max_calls_per_turn=3,
        repeat_guard=True,
    ))
