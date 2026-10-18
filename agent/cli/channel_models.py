"""The model a messaging channel answers with.

WeChat may use its own catalog model (``weixin.model`` in channels.json, ``/weixin model``).
Empty follows the default model: the one last chosen with ``/model`` in any window, which is
saved, so a backend started earlier (the desktop's channel host) still follows a later choice.
Both are read for each message; a change applies to the next one without a restart. A model
that is gone or lacks its key falls back to the backend's own, with a warning in the log.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from agent.channels.config import load_channels_config
from agent.cli.connections import is_local_url
from agent.cli.mode_preferences import apply_reasoning_effort
from agent.cli.model_catalog import CatalogEntry, ModelCatalog
from agent.cli.model_preferences import read_selected_model
from agent.runtime.llm import LLMClient, LLMConfig

logger = logging.getLogger(__name__)


def client_for(entry: CatalogEntry) -> LLMClient:
    """A client of its own for one catalog model, built the way the backend builds its first one."""
    profile = entry.profile
    api_key = profile.api_key()
    if not api_key:
        if is_local_url(entry.base_url) or not profile.api_key_env:
            api_key = "local"
        else:
            raise ValueError(f"Missing API key environment variable: {profile.api_key_env}")
    return LLMClient(apply_reasoning_effort(LLMConfig(
        provider=profile.provider, model=entry.model_id, api_key=api_key, base_url=entry.base_url,
        capabilities=profile.capabilities, context_limit=profile.context_limit, **profile.generation_settings(),
    )))


class ChannelModels:
    def __init__(self, catalog: Callable[[], ModelCatalog], current: Callable[[], str] = lambda: "") -> None:
        self._catalog = catalog
        # The catalog key of the model this backend answers its window with.
        self._current = current
        self._clients: dict[str, tuple[CatalogEntry, LLMClient]] = {}

    def resolve(self, key: str) -> CatalogEntry | None:
        catalog = self._catalog()
        return catalog.resolve(key) or catalog.resolve_persisted(key)

    @staticmethod
    def configured(channel: str) -> str:
        return load_channels_config().weixin.model if channel == "weixin" else ""

    def default(self) -> CatalogEntry | None:
        return self.resolve(read_selected_model() or "") or self.resolve(self._current())

    def client(self, channel: str) -> LLMClient | None:
        """The channel's client, or None to answer with the backend's own."""
        configured = self.configured(channel)
        entry = self.resolve(configured) if configured else self.default()
        if entry is None:
            if configured:
                logger.warning("channel model %s is not in the model catalog; answering with the default", configured)
            return None
        if entry.key == self._current():
            return None
        cached = self._clients.get(entry.key)
        if cached is None or cached[0] != entry:
            try:
                cached = self._clients[entry.key] = (entry, client_for(entry))
            except ValueError as exc:
                logger.warning("channel model %s is unavailable (%s); answering with the default", entry.key, exc)
                return None
        return cached[1]
