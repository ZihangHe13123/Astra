"""Play streamed 16-bit mono PCM on this machine's default output."""
from __future__ import annotations

import asyncio
import os
import struct
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from .provider import SpeechError


INSTALL_HINT = (
    "Audio output is not installed. Run `astra setup --extra voice` (in a source checkout: "
    "`uv sync --inexact --extra voice`), then restart Astra."
)


class Player(Protocol):
    def lead(self, seconds: float) -> None: ...
    def write(self, pcm: bytes) -> None: ...
    def unit_done(self) -> None: ...
    def stop(self) -> None: ...
    async def drain(self) -> None: ...
    def close(self) -> None: ...


class NullPlayer:
    """Accepts audio and plays nothing: for hosts without an output device and for tests."""

    def __init__(self) -> None:
        self.received = 0

    def lead(self, seconds: float) -> None: ...

    def write(self, pcm: bytes) -> None:
        self.received += len(pcm)

    def unit_done(self) -> None: ...

    def stop(self) -> None: ...

    async def drain(self) -> None: ...

    def close(self) -> None: ...


class SoundDevicePlayer:
    """One output stream fed from a queue of units.

    Synthesis can be slower than playback. A unit therefore starts only once a
    lead of its audio is buffered, and waits again if it runs dry. The caller
    sizes the lead to how fast audio is arriving, so a slow engine yields a
    pause before a sentence instead of stutter inside it.
    """

    def __init__(self, sample_rate: int, prebuffer_seconds: float) -> None:
        try:
            import sounddevice  # optional: installed with the "voice" extra
        except (ImportError, OSError) as error:
            raise SpeechError(INSTALL_HINT) from error
        self._rate = sample_rate
        self._floor = self._lead = int(sample_rate * prebuffer_seconds) * 2
        self._lock = threading.Lock()
        self._units: deque[list] = deque()  # [audio not yet played, whether all of it has arrived]
        self._open = False  # the gate between the unit at the head and the device
        try:
            self._stream = sounddevice.RawOutputStream(samplerate=sample_rate, channels=1, dtype="int16",
                                                       callback=self._fill)
            self._stream.start()
        except Exception as error:  # noqa: BLE001 - PortAudio reports device problems with its own types
            raise SpeechError(f"No usable audio output device: {error}") from error

    def _fill(self, outdata, frames, _time, _status) -> None:  # runs on the audio thread
        need, filled = frames * 2, 0
        with self._lock:
            while filled < need and self._units:
                audio, complete = self._units[0]
                if not audio:
                    self._open = False
                    if not complete:
                        break  # the engine fell behind: wait for a new lead
                    self._units.popleft()
                    continue
                if not self._open and not complete and len(audio) < self._lead:
                    break
                self._open = True
                take = min(need - filled, len(audio))
                outdata[filled:filled + take] = audio[:take]
                del audio[:take]
                filled += take
        outdata[filled:need] = bytes(need - filled)

    def lead(self, seconds: float) -> None:
        with self._lock:
            self._lead = max(self._floor, int(self._rate * seconds) * 2)

    def write(self, pcm: bytes) -> None:
        with self._lock:
            if not self._units or self._units[-1][1]:
                self._units.append([bytearray(), False])
            self._units[-1][0] += pcm

    def unit_done(self) -> None:
        with self._lock:
            if self._units:
                self._units[-1][1] = True

    def stop(self) -> None:
        with self._lock:
            self._units.clear()
            self._open = False

    async def drain(self) -> None:
        self.unit_done()
        while True:
            with self._lock:
                if not self._units:
                    break
            await asyncio.sleep(0.03)
        latency = self._stream.latency  # a pair only for duplex streams
        await asyncio.sleep((latency if isinstance(latency, float) else 0.0) + 0.05)

    def close(self) -> None:
        self._stream.abort()
        self._stream.close()


def open_player(sample_rate: int, prebuffer_seconds: float) -> Player:
    if os.getenv("ASTRA_VOICE_PLAYER", "").strip().lower() == "null":
        return NullPlayer()
    return SoundDevicePlayer(sample_rate, prebuffer_seconds)


class PhonePlayer:
    """Plays on a paired phone instead of this computer.

    Each unit's audio becomes a WAV file in `folder`, which the desktop serves to the phone, and a small
    announcement (id and length, no audio) goes out as an event. The phone plays the files in order.
    """

    KEEP_SECONDS = 600

    def __init__(self, sample_rate: int, folder: Path, announce: Callable[[dict[str, Any]], None], *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._rate, self._folder, self._announce, self._clock = sample_rate, folder, announce, clock
        self._pcm = bytearray()
        self._until = 0.0  # when the phone will have played what was sent, as estimated here
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._sweep()

    def lead(self, seconds: float) -> None:
        pass  # the phone receives whole units, so there is nothing to hold back

    def write(self, pcm: bytes) -> None:
        self._pcm += pcm

    def unit_done(self) -> None:
        if not self._pcm:
            return
        name = uuid.uuid4().hex
        data = bytes(self._pcm)
        self._pcm = bytearray()
        header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack(
            "<IHHIIHH", 16, 1, 1, self._rate, self._rate * 2, 2, 16) + b"data" + struct.pack("<I", len(data))
        temporary = self._folder / f".{name}.tmp"
        temporary.write_bytes(header + data)
        os.chmod(temporary, 0o600)
        temporary.replace(self._folder / f"{name}.wav")
        seconds = len(data) / 2 / self._rate
        self._until = max(self._until, self._clock()) + seconds
        self._announce({"id": name, "seconds": round(seconds, 3), "at": time.time()})

    def stop(self) -> None:
        """Silence the phone too, if it is still playing; called after a normal end as well, when it is not."""
        self._pcm = bytearray()
        if self._until > self._clock():
            self._announce({"stop": True, "at": time.time()})
        self._until = 0.0

    async def drain(self) -> None:
        # The phone fetches and decodes each unit before playing it: allow it a moment beyond the audio itself.
        if (wait := self._until - self._clock()) > 0:
            await asyncio.sleep(wait + 1.0)

    def close(self) -> None:
        pass

    def _sweep(self) -> None:
        cutoff = time.time() - self.KEEP_SECONDS
        for path in self._folder.glob("*.wav"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
            except OSError:
                pass
