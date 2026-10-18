"""Tools that act on the messaging channel for the current agent turn."""

from __future__ import annotations

from collections.abc import Awaitable
from pathlib import Path
from typing import Callable

from agent.runtime.tool_failure import ToolFailure
from agent.runtime.tools.registry import ToolDef, ToolRegistry

from .manager import ChannelManager
from .router import active_channel_message, channel_turn_media
from .weixin import spoken_text

_SPEAK_MAX_CHARS = 600


_SENSITIVE_NAMES = {
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "cookies",
    "login data",
}
_SENSITIVE_SUFFIXES = {".key", ".pem", ".pfx", ".p12"}
# Application data on macOS and Windows: tokens and browser stores live there.
_PRIVATE_DIRS = {"library", "appdata"}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _sensitive(path: Path, root: Path) -> bool:
    """Below a send root: hidden files and folders (.ssh, .env, .git, Astra's own .astra with the
    channel logins), application data, and files named or shaped like credentials."""
    parts = path.relative_to(root).parts
    return (
        any(part.startswith(".") for part in parts)
        or any(part.lower() in _PRIVATE_DIRS for part in parts[:-1])
        or path.name.lower() in _SENSITIVE_NAMES
        or path.suffix.lower() in _SENSITIVE_SUFFIXES
    )


def register_channel_tools(
    registry: ToolRegistry,
    manager_getter: Callable[[], ChannelManager | None],
    speaker: Callable[[str], Awaitable[tuple[bytes, int]]] | None = None,
) -> None:
    def unconfirmed(channel: str, what: str) -> ToolFailure:
        return ToolFailure(
            code="channel_send_unconfirmed",
            message=f"The {channel} channel did not confirm {what}; it may or may not have reached the user.",
            retryable=False,
            recovery_hint=(
                "Do not send it again on your own. Tell the user it was not confirmed "
                "and ask whether it arrived."
            ),
            partial=True,
        )

    async def send_voice(message, manager: ChannelManager, words: str) -> dict[str, object] | ToolFailure:
        from agent.speech.provider import SpeechError

        if message.channel != "weixin":
            raise RuntimeError("voice notes are only available in WeChat")
        text = spoken_text(words)
        if not text:
            raise ValueError("speak has nothing to say once emoji and markup are removed")
        if len(text) > _SPEAK_MAX_CHARS:
            raise ValueError(f"speak is {len(text)} characters; a voice note takes at most {_SPEAK_MAX_CHARS}")
        if speaker is None:
            return ToolFailure(code="voice_unavailable", message="No voice is set up on this computer.",
                               retryable=False, recovery_hint="Answer in text instead.")
        try:
            pcm, rate = await speaker(text)
        except SpeechError as exc:
            return ToolFailure(code="voice_unavailable", message=f"The voice could not speak: {exc}",
                               retryable=False, recovery_hint="Answer in text instead.")
        try:
            result = await manager.send_voice(message, pcm, rate)
        except TimeoutError:
            return unconfirmed(message.channel, "the voice note")
        media = channel_turn_media.get()
        if media is not None:
            media.append("voice")
        return {"status": "sent", "channel": message.channel, "kind": "voice", "seconds": result.get("seconds")}

    async def channel_send_file(path: str = "", name: str = "", speak: str = "") -> dict[str, object] | ToolFailure:
        message = active_channel_message.get()
        if message is None:
            raise RuntimeError("channel_send_file is only available during a messaging-channel turn")
        if bool(path.strip()) == bool(speak.strip()):
            raise ValueError("give either path (a local file) or speak (words for a voice note)")
        manager = manager_getter()
        if manager is None:
            raise RuntimeError("messaging channel manager is not ready")
        policy = manager.file_policy(message.channel)
        if policy is None:
            raise RuntimeError(f"the {message.channel} channel cannot send files")
        if not policy.enabled:
            raise PermissionError("channel file sending is disabled")
        if not policy.recipients or message.sender_id not in policy.recipients:
            raise PermissionError("sender is not allowed to receive local files")
        if speak.strip():
            return await send_voice(message, manager, speak)

        try:
            source = Path(path).expanduser().resolve(strict=True)
        except FileNotFoundError:
            # The OS error names the first missing directory, not the path that was asked for.
            raise FileNotFoundError(f"file not found: {path}") from None
        if not source.is_file():
            raise ValueError(f"not a file: {source}")
        roots = [Path(item).expanduser().resolve() for item in policy.roots]
        root = next((item for item in roots if _inside(source, item)), None)
        if root is None:
            raise PermissionError("file is outside configured send_file_roots")
        if _sensitive(source, root):
            raise PermissionError("refusing to send a potentially sensitive credential file")
        size = source.stat().st_size
        if size > policy.max_bytes:
            raise ValueError(
                f"file is too large: {size} bytes > {policy.max_bytes} bytes"
            )
        upload_name = Path(name).name if name.strip() else source.name
        try:
            await manager.send_file(message, str(source), upload_name)
        except TimeoutError:
            # The upload was handed to the channel and no confirmation came back.
            return unconfirmed(message.channel, f"the upload of {upload_name}")
        media = channel_turn_media.get()
        if media is not None:
            media.append("file")
        return {
            "status": "sent",
            "channel": message.channel,
            "name": upload_name,
            "bytes": size,
        }

    registry.register(ToolDef(
        name="channel_send_file",
        description=(
            "Send an existing local file to the user in the active QQ or WeChat conversation; "
            "an image or video arrives as one, anything else as a file. In WeChat, speak instead "
            "of path sends a voice note of those words in Astra's voice: use it when the user asks "
            "to hear you or for a voice message. "
            "Works only in a turn that came from a messaging channel. "
            "Use only when that user explicitly asks to receive a file. The path must be inside "
            "configured send_file_roots; hidden files and folders, application data and credential "
            "files are always blocked. "
            "At most 3 calls per turn: a further call is not run."
        ),
        parameters={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Absolute path of the existing local file to send.",
                    "default": "",
                },
                "speak": {
                    "type": "string",
                    "description": (
                        "WeChat only, instead of path: the words to say in a voice note, as spoken "
                        "sentences without emoji or Markdown. Do not repeat them in text."
                    ),
                    "default": "",
                },
                "name": {
                    "type": "string",
                    "description": "Optional filename shown to the recipient.",
                    "default": "",
                },
            },
            "required": [],
        },
        fn=channel_send_file,
        risk="write",
        # Sends the file; the file itself stays as it is.
        writes_paths=False,
        approval="never",
        timeout=45,
        max_calls_per_turn=3,
        repeat_guard=True,
    ))
