"""Voice settings as they appear under "voice" in the preferences document."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping
from urllib.parse import urlsplit


@dataclass(frozen=True)
class VoiceProfile:
    """One selectable voice: the provider's own voice id, extra request fields, or both."""

    name: str
    voice: str = ""
    request: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SpeechSettings:
    enabled: bool = False
    selected: str = ""
    base_url: str = ""
    connection: str = ""  # a provider connection whose endpoint and key to reuse
    api_key_env: str = ""
    model: str = ""
    sample_rate: int = 24000
    request: Mapping[str, Any] = field(default_factory=dict)
    first_request: Mapping[str, Any] = field(default_factory=dict)  # overrides for a reply's opening unit
    voices: tuple[VoiceProfile, ...] = ()
    server_command: tuple[str, ...] = ()
    server_idle_seconds: float = 600.0
    server_startup_seconds: float = 90.0
    max_chars: int = 600
    prebuffer_seconds: float = 0.25

    @classmethod
    def parse(cls, value: object) -> SpeechSettings:
        """Read what is valid and keep the default for everything else."""
        def fields(item: object) -> dict[str, Any]:
            return dict(item) if isinstance(item, dict) else {}

        def text(source: Mapping[str, Any], key: str) -> str:
            item = source.get(key)
            return item.strip() if isinstance(item, str) else ""

        def number(source: Mapping[str, Any], key: str, default: float, low: float, high: float) -> float:
            item = source.get(key)
            return float(item) if isinstance(item, (int, float)) and not isinstance(item, bool) and low <= item <= high else default

        data, server, voices = fields(value), fields(fields(value).get("server")), fields(fields(value).get("voices"))
        command = server.get("command")
        return cls(
            enabled=data.get("enabled") is True,
            selected=text(data, "selected"),
            base_url=text(data, "base_url").rstrip("/"),
            connection=text(data, "connection"),
            api_key_env=text(data, "api_key_env"),
            model=text(data, "model"),
            sample_rate=int(number(data, "sample_rate", 24000, 8000, 192000)),
            request=fields(data.get("request")),
            first_request=fields(data.get("first_request")),
            voices=tuple(
                VoiceProfile(name, text(fields(item), "voice"), fields(fields(item).get("request")))
                for name, item in voices.items() if isinstance(name, str) and name.strip()
            ),
            server_command=tuple(command) if isinstance(command, list) and command and all(
                isinstance(part, str) and part for part in command) else (),
            server_idle_seconds=number(server, "idle_seconds", 600.0, 10, 86400),
            server_startup_seconds=number(server, "startup_seconds", 90.0, 1, 900),
            max_chars=int(number(data, "max_chars", 600, 20, 100000)),
            prebuffer_seconds=number(data, "prebuffer_seconds", 0.25, 0, 5),
        )

    @property
    def profile(self) -> VoiceProfile:
        """The selected voice, else the first one, else the provider's default voice."""
        return next((voice for voice in self.voices if voice.name == self.selected),
                    self.voices[0] if self.voices else VoiceProfile(""))

    @property
    def problem(self) -> str:
        """Why speech cannot run with these settings; empty when it can."""
        if not self.base_url and not self.connection:
            return "Voice is not set up: add voice.base_url (an OpenAI-compatible speech endpoint) to settings."
        if self.base_url:
            parsed = urlsplit(self.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return "voice.base_url must be an HTTP(S) URL such as http://127.0.0.1:8000/v1."
        return ""
