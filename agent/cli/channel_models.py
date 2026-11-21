"""The model a messaging channel answers with, and how hard it thinks.

WeChat may use its own catalog model (``weixin.model`` in channels.json, ``/weixin model``) and its own
reasoning effort (``weixin.reasoning_effort``, ``/weixin effort``). Empty follows the default: the model last
chosen with ``/model`` and the effort last chosen with ``/mode`` in any window, both saved, so a backend
started earlier (the desktop's channel host) still follows a later choice. All are read for each message; a
change applies to the next one without a restart. A model that is gone or lacks its key falls back to the
window's model, still at the channel's effort, with a warning in the log.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from agent.channels.config import WeixinConfig, load_channels_config
from agent.cli.connections import is_local_url
from agent.cli.mode_preferences import apply_reasoning_effort, read_reasoning_effort
from agent.cli.model_catalog import CatalogEntry, ModelCatalog
from agent.cli.model_preferences import read_selected_model
from agent.runtime.llm import LLMClient, LLMConfig

if TYPE_CHECKING:
    from agent.channels.manager import ChannelManager

logger = logging.getLogger(__name__)


def _api_key(entry: CatalogEntry) -> str:
    """The key a client of this model sends; ValueError when the one it needs is not set."""
    profile = entry.profile
    api_key = profile.api_key()
    if not api_key:
        if is_local_url(entry.base_url) or not profile.api_key_env:
            api_key = "local"
        else:
            raise ValueError(f"Missing API key environment variable: {profile.api_key_env}")
    return api_key


def client_for(entry: CatalogEntry, effort: str | None = None) -> LLMClient:
    """A client of its own for one catalog model, built the way the backend builds its first one, at
    ``effort`` (the one /mode saved when None)."""
    profile = entry.profile
    api_key = _api_key(entry)
    return LLMClient(apply_reasoning_effort(LLMConfig(
        provider=profile.provider, model=entry.model_id, api_key=api_key, base_url=entry.base_url,
        capabilities=profile.capabilities, context_limit=profile.context_limit, **profile.generation_settings(),
    ), effort))


class ChannelModels:
    def __init__(self, catalog: Callable[[], ModelCatalog], current: Callable[[], str] = lambda: "",
                 channels: Callable[[], ChannelManager | None] = lambda: None,
                 current_effort: Callable[[], str] = read_reasoning_effort) -> None:
        self._catalog = catalog
        # The backend's channels: a running WeChat channel's own settings say which model it answers with.
        self._channels = channels
        # The catalog key of the model this backend answers its window with, and that client's effort.
        self._current = current
        self._current_effort = current_effort
        self._clients: dict[tuple[str, str], tuple[CatalogEntry, LLMClient]] = {}
        # WeChat's settings last read from channels.json, kept while the file cannot be read.
        self._saved: WeixinConfig | None = None

    def resolve(self, key: str) -> CatalogEntry | None:
        catalog = self._catalog()
        return catalog.resolve(key) or catalog.resolve_persisted(key)

    def _settings(self, channel: str) -> WeixinConfig | None:
        """The settings of the channel's next reply. A running WeChat channel reads channels.json as each
        message arrives and keeps the settings it last applied while the file cannot be read; they are the ones
        used, and the ones /weixin status shows. Without one, the file is read here, with the same rule."""
        if channel != "weixin":
            return None
        manager = self._channels()
        adapter = manager.weixin() if manager is not None else None
        if adapter is not None:
            return adapter.settings()
        try:
            self._saved = load_channels_config(strict=True).weixin
        except (OSError, ValueError):
            if self._saved is None:
                self._saved = load_channels_config().weixin
        return self._saved

    def configured(self, channel: str) -> str:
        """The channel's own model (a catalog key), or "" to follow the default."""
        settings = self._settings(channel)
        return settings.model if settings is not None else ""

    def configured_effort(self, channel: str) -> str:
        """The channel's own reasoning effort, or "" to follow /mode."""
        settings = self._settings(channel)
        return settings.reasoning_effort if settings is not None else ""

    def effort(self, channel: str) -> str:
        """The reasoning effort of the channel's next reply: its own, or the one /mode saved last."""
        return self.configured_effort(channel) or read_reasoning_effort()

    def default(self) -> CatalogEntry | None:
        return self.resolve(read_selected_model() or "") or self.resolve(self._current())

    def _candidates(self, channel: str) -> list[CatalogEntry]:
        """The models the channel's next reply may go to, in order: its own or the default, then the window's."""
        configured = self.configured(channel)
        chosen = self.resolve(configured) if configured else self.default()
        if chosen is None and configured:
            logger.warning("channel model %s is not in the model catalog; answering with the window's", configured)
        window = self.resolve(self._current())
        candidates = [chosen] if chosen is not None else []
        if window is not None and (chosen is None or window.key != chosen.key):
            candidates.append(window)
        return candidates

    def answering(self, channel: str) -> CatalogEntry | None:
        """The model the channel's next reply goes to: the first that is in the catalog and has its key."""
        for entry in self._candidates(channel):
            try:
                _api_key(entry)
            except ValueError:
                continue
            return entry
        return None

    def client(self, channel: str) -> LLMClient | None:
        """The channel's client, or None to answer with the window's own. A model that cannot be used gives way
        to the window's model, which still thinks at the channel's effort."""
        effort = self.effort(channel)
        for entry in self._candidates(channel):
            if entry.key == self._current() and effort == self._current_effort():
                return None
            try:
                return self._client(entry, effort)
            except ValueError as exc:
                logger.warning("channel model %s is unavailable (%s); answering with the window's", entry.key, exc)
        return None

    def _client(self, entry: CatalogEntry, effort: str) -> LLMClient:
        cached = self._clients.get((entry.key, effort))
        # A cached client is used only while it is still what it was built as: nothing outside a channel turn
        # decides how a channel thinks, even if something changed that client in place.
        if (cached is None or cached[0] != entry or cached[1].config.model != entry.model_id
                or cached[1].config.reasoning_effort != effort):
            cached = self._clients[(entry.key, effort)] = (entry, client_for(entry, effort))
        return cached[1]
