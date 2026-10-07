"""Speak the assistant's reply as it streams, and stop the moment the user moves on."""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable, Protocol

import httpx

from .player import Player, open_player
from .provider import OpenAICompatibleSpeech, SpeechError
from .server import LocalServer
from .settings import SpeechSettings
from .text import SpeechSegmenter, spoken_seconds

logger = logging.getLogger(__name__)


class VoiceStore(Protocol):
    """Where settings live and how an endpoint's address and key are resolved."""

    def load(self) -> SpeechSettings: ...
    def endpoint(self, settings: SpeechSettings) -> tuple[str, str]: ...
    def runtime_dir(self) -> Path: ...


class SpeechService:
    """One reply at a time: text in through `feed`, audio out in order.

    Nothing here reads or writes the conversation. Units are synthesized one
    after another, each while the audio before it plays: a local engine gives
    one request all its speed, and a fast one gets ahead of playback anyway.
    """

    def __init__(self, store: VoiceStore, notify: Callable[[dict[str, Any]], None], *,
                 open_player: Callable[[int, float], Player] = open_player) -> None:
        self._store, self._notify, self._open_player = store, notify, open_player
        self.settings = store.load()
        self._segmenter: SpeechSegmenter | None = None
        self._units: asyncio.Queue[str | None] = asyncio.Queue()
        self._runner: asyncio.Task[None] | None = None
        self._player: Player | None = None
        self._client: httpx.AsyncClient | None = None
        self._server: LocalServer | None = None
        self._closing: asyncio.Task[None] | None = None
        self._budget = 0
        self._speed = 1.0  # audio seconds arriving per second, as last measured
        self._state = "idle"
        self._error = ""
        self._reported: dict[str, Any] = {}

    # ── what the backend calls ──

    def reload(self) -> None:
        self.settings = self._store.load()
        self._error = ""
        if not self.settings.enabled:
            self.stop()
            if self._server is not None:  # switched off: give the model's memory back now, not after the idle wait
                server, self._server = self._server, None
                self._closing = asyncio.get_running_loop().create_task(server.close())

    def begin_turn(self) -> None:
        """A new reply is coming: drop the old one and get the engine ready while the model thinks."""
        self.stop()
        self.settings = self._store.load()
        if self.settings.enabled and not self.settings.problem:
            self._start()

    def feed(self, text: str) -> None:
        if self._segmenter is not None:
            for unit in self._segmenter.feed(text):
                self._say(unit)

    def finish(self) -> None:
        """The reply is complete: say what is left, then fall silent."""
        if self._segmenter is not None:
            for unit in self._segmenter.flush():
                self._say(unit)
            self._segmenter = None
            self._units.put_nowait(None)

    def say(self, text: str) -> None:
        """Speak one standalone text now, whether or not replies are being read aloud."""
        self.stop()
        self.settings = self._store.load()
        if problem := self.settings.problem:
            raise SpeechError(problem)
        self._start()
        self.feed(text)
        self.finish()

    def stop(self) -> None:
        """Silence now: pending text, synthesis in flight and buffered audio are all dropped."""
        self._segmenter = None
        if self._runner is not None:
            self._runner.cancel()
            self._runner = None
        if self._player is not None:
            self._player.stop()
        self._units = asyncio.Queue()
        self._set_state("idle")

    @property
    def speaking(self) -> bool:
        return self._runner is not None and not self._runner.done()

    def status(self) -> dict[str, Any]:
        settings = self.settings
        return {
            "enabled": settings.enabled,
            "state": self._state if settings.enabled or self.speaking else "off",
            "voice": settings.profile.name,
            "voices": [voice.name for voice in settings.voices],
            "error": self._error or (settings.problem if settings.enabled else ""),
        }

    def report(self, *, force: bool = False, message: str | None = None, error: str = "") -> None:
        """Send the status when it changed; a command's answer carries `message` and is always sent."""
        status = self.status()
        if force or status != self._reported:
            self._reported = status
            self._notify({**status, **({} if message is None else {"message": message}), **({"error": error} if error else {})})

    async def close(self) -> None:
        runner = self._runner
        self.stop()
        if runner is not None:
            with suppress(asyncio.CancelledError, Exception):
                await runner
        if self._closing is not None:
            await self._closing
        if self._server is not None:
            await self._server.close()
        if self._client is not None:
            await self._client.aclose()

    # ── inside ──

    def _start(self) -> None:
        self._segmenter = SpeechSegmenter()
        self._budget = self.settings.max_chars
        self._error = ""
        self._runner = asyncio.get_running_loop().create_task(self._run(self.settings, self._units))

    def _say(self, unit: str) -> None:
        if self._budget > 0:  # a long reply is read up to the limit, not to the end
            self._budget -= len(unit)
            self._units.put_nowait(unit)

    def _set_state(self, state: str) -> None:
        if state != self._state:
            self._state = state
            self.report()

    async def _provider(self, settings: SpeechSettings) -> OpenAICompatibleSpeech:
        base_url, api_key = self._store.endpoint(settings)
        if self._client is None:
            self._client = httpx.AsyncClient()
        provider = OpenAICompatibleSpeech(self._client, base_url=base_url, api_key=api_key, model=settings.model,
                                          request=settings.request, first_request=settings.first_request)
        if settings.server_command:
            runtime = self._store.runtime_dir()
            if self._server is None or self._server.command != settings.server_command:
                if self._server is not None:
                    await self._server.close()
                self._server = LocalServer(settings.server_command, heartbeat=runtime / "server.heartbeat",
                                           log=runtime / "server.log", idle_seconds=settings.server_idle_seconds,
                                           startup_seconds=settings.server_startup_seconds)
            if not await provider.healthy():
                self._set_state("starting")
            await self._server.start(provider.healthy)
        return provider

    async def _run(self, settings: SpeechSettings, units: asyncio.Queue[str | None]) -> None:
        voice = settings.profile
        player: Player | None = None
        first = True
        try:
            provider = await self._provider(settings)
            while (text := await units.get()) is not None:
                self._set_state("speaking")  # from the first unit on, so the user can stop it before any sound
                if self._server is not None:
                    self._server.touch()
                expected, arrived, since, before = spoken_seconds(text), 0.0, 0.0, 0.0
                async for pcm in provider.stream(text, voice, first=first):  # a failed unit ends this reply's speech
                    if player is None:
                        player = self._player = self._open_player(settings.sample_rate, settings.prebuffer_seconds)
                    now = asyncio.get_running_loop().time()
                    arrived += len(pcm) / 2 / settings.sample_rate
                    if not since:
                        since, before = now, arrived  # the first piece was made before the clock started
                    elif now - since > 0.5:
                        self._speed = (arrived - before) / (now - since)
                    # Hold back what a slower-than-speech engine will still owe by the end of the unit.
                    # Its length is a rough estimate and its speed wavers, so assume another second
                    # to come and a tenth less speed: a longer pause is better than a broken word.
                    player.lead(max(expected - arrived, 1.0) * max(1.0 - 0.9 * self._speed, 0.0))
                    player.write(pcm)
                first = False
                if player is not None:
                    player.unit_done()
            if player is not None:
                await player.drain()
        except SpeechError as error:
            logger.warning("voice: %s", error)
            self._error = str(error)
        except Exception as error:  # noqa: BLE001 - speech must never take the reply down with it
            logger.exception("voice failed")
            self._error = f"Voice failed: {type(error).__name__}"
        finally:
            if player is not None:
                player.stop()
                player.close()
                if self._player is player:
                    self._player = None
            if self._runner is asyncio.current_task():
                self._runner = None
                self._state = "idle"
                self.report()
