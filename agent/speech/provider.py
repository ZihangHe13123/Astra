"""Text to audio over the OpenAI-compatible speech endpoint."""
from __future__ import annotations

from typing import Any, AsyncIterator, Mapping

import httpx

from .settings import VoiceProfile


class SpeechError(Exception):
    """Speech could not be produced; the message is fit to show the user."""


class OpenAICompatibleSpeech:
    """`POST {base_url}/audio/speech`, answered with raw 16-bit mono PCM.

    Local model servers and hosted APIs share this request. Whatever they add to
    it (reference audio for a cloned voice, streaming switches) travels in the
    `request` fields of the settings and of each voice. `first_request` applies
    on top for a reply's opening unit, where starting soon matters more than
    throughput.
    """

    def __init__(self, client: httpx.AsyncClient, *, base_url: str, api_key: str = "", model: str = "",
                 request: Mapping[str, Any] | None = None, first_request: Mapping[str, Any] | None = None) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._model = model
        self._request, self._first_request = dict(request or {}), dict(first_request or {})

    def body(self, text: str, voice: VoiceProfile, *, first: bool = False) -> dict[str, Any]:
        body: dict[str, Any] = {"input": text, "response_format": "pcm", **self._request,
                                **(self._first_request if first else {})}
        if self._model:
            body["model"] = self._model
        if voice.voice:
            body["voice"] = voice.voice
        return {**body, **voice.request}

    async def stream(self, text: str, voice: VoiceProfile, *, first: bool = False) -> AsyncIterator[bytes]:
        """Yield audio as it arrives, always in whole 16-bit samples."""
        timeout = httpx.Timeout(connect=5, read=120, write=10, pool=5)
        try:
            async with self._client.stream("POST", f"{self._base_url}/audio/speech",
                                           json=self.body(text, voice, first=first),
                                           headers=self._headers, timeout=timeout) as response:
                if response.status_code != 200:
                    detail = (await response.aread())[:200].decode("utf-8", "replace").strip()
                    raise SpeechError(f"Speech endpoint answered {response.status_code}: {detail}")
                odd = b""
                async for chunk in response.aiter_bytes():
                    data = odd + chunk
                    whole = len(data) & ~1
                    odd = data[whole:]
                    if whole:
                        yield data[:whole]
        except httpx.HTTPError as error:
            raise SpeechError(f"Speech endpoint is unreachable: {type(error).__name__}") from error

    async def healthy(self) -> bool:
        """True once the server answers HTTP at all; a missing /models route is still a live server."""
        try:
            response = await self._client.get(f"{self._base_url}/models", headers=self._headers, timeout=2)
        except httpx.HTTPError:
            return False
        return response.status_code < 500
