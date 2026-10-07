"""Voice settings in the preferences document, and the /voice command."""

import json
import os
from pathlib import Path

from agent.runtime.json_preferences import update_preferences
from agent.runtime.paths import state_path
from agent.speech.provider import SpeechError
from agent.speech.service import SpeechService
from agent.speech.settings import SpeechSettings

from .model_preferences import PROJECT_ROOT, model_settings_path
from .provider_connections import read_connections

USAGE = "Usage: /voice [on|off|stop|list|use <voice>|test [text]]"
SAMPLE = "你好，我在这里。Voice output is working."


class VoicePreferences:
    """The "voice" section of settings.json, shared by every session of this installation."""

    def load(self) -> SpeechSettings:
        try:
            data = json.loads(model_settings_path().read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            data = {}
        return SpeechSettings.parse(data.get("voice") if isinstance(data, dict) else None)

    def save(self, **changes: object) -> Path:
        def change(data: dict) -> None:
            voice = data.get("voice")
            data["voice"] = {**(voice if isinstance(voice, dict) else {}), **changes}

        return update_preferences(model_settings_path(), change)

    def endpoint(self, settings: SpeechSettings) -> tuple[str, str]:
        """The speech endpoint and its key: the voice's own, else a saved provider connection's."""
        base_url = settings.base_url
        api_key = os.getenv(settings.api_key_env, "").strip() if settings.api_key_env else ""
        if settings.connection:
            record = read_connections().get(settings.connection)
            if record is None:
                raise SpeechError(f"voice.connection names no saved connection: {settings.connection}")
            base_url = base_url or str(record.get("base_url") or "").rstrip("/")
            api_key = api_key or str(record.get("api_key") or "") or os.getenv(str(record.get("api_key_env") or ""), "")
        return base_url, api_key

    def runtime_dir(self) -> Path:
        return state_path("voice", root=PROJECT_ROOT)


def _summary(service: SpeechService) -> str:
    status = service.status()
    if not status["enabled"]:
        return "Voice is off. /voice on reads replies aloud."
    voice = f" · {status['voice']}" if status["voice"] else ""
    return f"Voice is on{voice}."


def execute_voice_command(service: SpeechService, store: VoicePreferences, args: list[str]) -> tuple[str, str]:
    """Apply one /voice command and return (message, error)."""
    action = args[0].lower() if args else "status"
    try:
        if action == "status" and len(args) <= 1:
            service.reload()
            return _summary(service), service.status()["error"]
        if action in {"on", "off"} and len(args) == 1:
            store.save(enabled=action == "on")
            service.reload()
            return _summary(service), service.status()["error"]
        if action == "stop" and len(args) == 1:
            service.stop()
            return "Stopped speaking.", ""
        if action == "list" and len(args) == 1:
            service.reload()
            status = service.status()
            names = [f"{name} (selected)" if name == status["voice"] else name for name in status["voices"]]
            return ("Voices: " + ", ".join(names) if names else "No voices are defined; the provider's default is used."), ""
        if action == "use" and len(args) > 1:
            wanted = " ".join(args[1:])
            names = [voice.name for voice in store.load().voices]
            matches = [name for name in names if name == wanted] or [
                name for name in names if name.lower().startswith(wanted.lower())]
            if len(matches) != 1:
                return "", f"Unknown voice: {wanted}. " + ("Voices: " + ", ".join(names) if names else "None are defined.")
            store.save(selected=matches[0])
            service.reload()
            return f"Voice set to {matches[0]}.", ""
        if action == "test":
            service.say(" ".join(args[1:]) or SAMPLE)
            return "Speaking a test line.", ""
    except (OSError, SpeechError) as error:
        return "", str(error)
    return "", USAGE
