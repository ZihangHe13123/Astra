"""Provider registry shared by the runtime and model configuration layer."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


ProviderFactory = Callable[[Any], Any]


class ProviderRegistry:
    """Resolve provider adapters by stable, configuration-friendly names."""

    def __init__(self):
        self._factories: dict[str, ProviderFactory] = {}

    def register(self, name: str, factory: ProviderFactory, *, replace: bool = False) -> None:
        key = self.normalize_name(name)
        if key in self._factories and not replace:
            raise ValueError(f"Provider already registered: {key}")
        self._factories[key] = factory

    def create(self, name: str, config: Any):
        key = self.normalize_name(name)
        factory = self._factories.get(key)
        if factory is None:
            available = ", ".join(self.names) or "(none)"
            raise ValueError(f"Unknown provider '{name}'. Available: {available}")
        return factory(config)

    @property
    def names(self) -> list[str]:
        return sorted(self._factories)

    @staticmethod
    def normalize_name(name: str) -> str:
        return (name or "openai-compatible").strip().lower().replace("_", "-")


DEFAULT_PROVIDER_REGISTRY = ProviderRegistry()


def _openai_compatible_factory(config):
    from .llm import OpenAICompatibleProvider

    return OpenAICompatibleProvider(config)


DEFAULT_PROVIDER_REGISTRY.register("openai-compatible", _openai_compatible_factory)


def _codex_factory(config):
    from .codex_provider import CodexProvider
    return CodexProvider(config)


DEFAULT_PROVIDER_REGISTRY.register("openai-codex", _codex_factory)
