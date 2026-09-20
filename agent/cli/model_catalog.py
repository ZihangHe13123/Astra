"""Dynamic model discovery grouped by OpenAI-compatible provider endpoints."""

from __future__ import annotations

import asyncio
import os
import logging
from urllib.parse import urlsplit
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import httpx

from agent.runtime.deepseek import LEGACY_DEEPSEEK_MODELS, canonical_deepseek_model

from . import model_cache
from .provider_connections import ROUTES, read_connections, record_profile

from .local_omlx import local_omlx_provider_spec
from .models import (
    DEFAULT_MODELS_PATH,
    PACKAGED_MODELS_PATH,
    USER_MODELS_PATH,
    ModelProfile,
    _load_yaml,
    _profile_from_data,
    model_profiles,
)


@dataclass(frozen=True)
class ProviderEndpoint:
    id: str
    label: str
    profile: ModelProfile
    discovery_timeout: float = 4.0
    inferred: bool = False


@dataclass(frozen=True)
class CatalogEntry:
    key: str
    model_id: str
    provider_id: str
    provider_label: str
    base_url: str
    profile: ModelProfile
    source: str = "preset"
    metadata_known: bool = True
    fetched_at: float | None = None

    def to_event(self, *, current: bool = False) -> dict[str, Any]:
        return {
            "key": self.key,
            "name": self.model_id,
            "provider": self.provider_label,
            "provider_id": self.provider_id,
            "endpoint": self.base_url,
            "context_limit": self.profile.context_limit,
            "current": current,
            "source": self.source,
            "metadata_known": self.metadata_known,
            "capabilities": sorted(self.profile.capabilities),
            "fetched_at": self.fetched_at,
        }


@dataclass(frozen=True)
class ModelCatalog:
    entries: tuple[CatalogEntry, ...]
    errors: dict[str, str]
    statuses: dict[str, str] = field(default_factory=dict)
    error_codes: dict[str, str] = field(default_factory=dict)

    @property
    def profiles(self) -> dict[str, ModelProfile]:
        return {entry.key: entry.profile for entry in self.entries}

    def resolve(self, value: str | None) -> CatalogEntry | None:
        if not value:
            return None
        exact = next((entry for entry in self.entries if entry.key == value), None)
        if exact:
            return exact
        matches = [entry for entry in self.entries if entry.model_id == value]
        if not matches:
            provider_id, _, model_id = value.rpartition("::")
            model_id = model_id or value
            matches = [
                entry for entry in self.entries
                if (not provider_id or provider_id == entry.provider_id)
                and canonical_deepseek_model(model_id, entry.base_url) == entry.model_id
            ]
        return matches[0] if len(matches) == 1 else None

    def resolve_persisted(self, value: str | None) -> CatalogEntry | None:
        """Resolve a saved dynamic model even when initial discovery is offline.

        The provider remains the trust boundary: a removed provider invalidates
        its saved selection, while a configured provider can recreate the last
        model profile without requiring a successful startup `/models` request.
        """
        resolved = self.resolve(value)
        if resolved or not value or "::" not in value:
            return resolved
        provider_id, model_id = value.split("::", 1)
        if not provider_id or not model_id or any(ord(c) < 32 or ord(c) == 127 for c in model_id):
            return None
        if provider_id == "configured":
            # Migrate selections saved before static remote profiles were
            # split into provider-specific API sources.
            static = model_profiles()
            static_profile = static.get(model_id)
            if static_profile is None and model_id in LEGACY_DEEPSEEK_MODELS:
                migrated = self.resolve(model_id)
                if migrated is not None and canonical_deepseek_model(model_id, migrated.base_url) != model_id:
                    return migrated
            if static_profile is not None:
                if static_profile.catalog_provider != "configured":
                    migrated_key = (
                        f"{static_profile.catalog_provider}::"
                        f"{static_profile.model_id or model_id}"
                    )
                    migrated = self.resolve(migrated_key)
                    if migrated is not None:
                        return migrated
                    return CatalogEntry(
                        key=migrated_key,
                        model_id=static_profile.model_id or model_id,
                        provider_id=static_profile.catalog_provider,
                        provider_label=static_profile.provider_label,
                        base_url=static_profile.base_url,
                        profile=replace(
                            static_profile,
                            model_id=static_profile.model_id or model_id,
                        ),
                    )
                endpoint = next(
                    (
                        item
                        for item in provider_endpoints()
                        if item.profile.base_url.rstrip("/")
                        == static_profile.base_url.rstrip("/")
                    ),
                    None,
                )
                if endpoint is not None:
                    migrated_key = f"{endpoint.id}::{static_profile.model_id or model_id}"
                    migrated = self.resolve(migrated_key)
                    if migrated is not None:
                        return migrated
                    profile = replace(
                        static_profile,
                        base_url=endpoint.profile.base_url,
                        api_key_env=endpoint.profile.api_key_env,
                        api_key_resolver=endpoint.profile.api_key_resolver,
                        model_id=static_profile.model_id or model_id,
                        catalog_provider=endpoint.id,
                        provider_label=endpoint.label,
                    )
                    return CatalogEntry(
                        key=migrated_key,
                        model_id=profile.model_id,
                        provider_id=endpoint.id,
                        provider_label=endpoint.label,
                        base_url=endpoint.profile.base_url,
                        profile=profile,
                    )
        endpoint = next((item for item in provider_endpoints() if item.id == provider_id), None)
        if endpoint is None:
            return None
        model_id = canonical_deepseek_model(model_id, endpoint.profile.base_url)
        value = f"{provider_id}::{model_id}"
        cached = model_cache.read_cache(endpoint)
        if cached:
            for entry in entries_from_items(endpoint, cached[0], source="cache", fetched_at=cached[1]):
                if entry.model_id == model_id:
                    return entry
        static = model_profiles()
        override = static.get(model_id)
        same_endpoint = (
            override is not None
            and override.base_url.rstrip("/") == endpoint.profile.base_url.rstrip("/")
        )
        template = override if override is not None and same_endpoint else endpoint.profile
        profile = replace(
            template,
            base_url=endpoint.profile.base_url,
            api_key_env=endpoint.profile.api_key_env,
            api_key_resolver=endpoint.profile.api_key_resolver,
            model_id=model_id,
            catalog_provider=endpoint.id,
            provider_label=endpoint.label,
        )
        return CatalogEntry(
            key=value,
            model_id=model_id,
            provider_id=endpoint.id,
            provider_label=endpoint.label,
            base_url=endpoint.profile.base_url,
            profile=profile,
            source="manual",
            metadata_known=same_endpoint or not endpoint.inferred,
        )


def configured_model_catalog() -> ModelCatalog:
    """Build a startup-safe catalog without probing provider endpoints.

    Live discovery remains available for an explicit model-menu refresh.  The
    persisted selection can also rebuild a dynamic entry through
    ``resolve_persisted`` when its provider is still configured.
    """
    endpoints = provider_endpoints()
    entries: dict[str, CatalogEntry] = {}

    for name, configured_profile in model_profiles().items():
        model_id = configured_profile.model_id or name
        endpoint = next(
            (
                item
                for item in endpoints
                if item.profile.base_url.rstrip("/")
                == configured_profile.base_url.rstrip("/")
            ),
            None,
        )
        if endpoint is not None:
            provider_id = endpoint.id
            provider_label = endpoint.label
            base_url = endpoint.profile.base_url
            profile = replace(
                configured_profile,
                base_url=base_url,
                api_key_env=endpoint.profile.api_key_env,
                api_key_resolver=endpoint.profile.api_key_resolver,
                model_id=model_id,
                catalog_provider=provider_id,
                provider_label=provider_label,
            )
        else:
            provider_id = configured_profile.catalog_provider or "configured"
            provider_label = configured_profile.provider_label or "Configured"
            base_url = configured_profile.base_url
            profile = replace(configured_profile, model_id=model_id)

        key = f"{provider_id}::{model_id}"
        # A legacy user alias must not replace the canonical bundled profile.
        if key in entries and name != model_id:
            continue
        entries[key] = CatalogEntry(
            key=key,
            model_id=model_id,
            provider_id=provider_id,
            provider_label=provider_label,
            base_url=base_url,
            profile=profile,
        )

    for endpoint in endpoints:
        cached = model_cache.read_cache(endpoint)
        if cached:
            for entry in entries_from_items(endpoint, cached[0], source="cache", fetched_at=cached[1]):
                entries.setdefault(entry.key, entry)
    return ModelCatalog(tuple(entries.values()), {})


def parse_model_command_argument(command: str) -> str:
    """Return the complete model selector after ``/model``.

    Dynamic provider keys may contain spaces (for example a llama.cpp model
    alias). Command tokenization must not silently truncate those keys.
    Matching outer quotes are accepted for the plain CLI as well.
    """
    parts = str(command).strip().split(maxsplit=1)
    if not parts or parts[0].lower() != "/model" or len(parts) == 1:
        return ""
    argument = parts[1].strip()
    if len(argument) >= 2 and argument[0] == argument[-1] and argument[0] in {'"', "'"}:
        argument = argument[1:-1].strip()
    return argument


def _configured_paths() -> tuple[Path, Path]:
    bundled = DEFAULT_MODELS_PATH if DEFAULT_MODELS_PATH.exists() else PACKAGED_MODELS_PATH
    configured = Path(os.getenv("AGENT_MODELS_FILE", str(bundled))).expanduser()
    user = Path(os.getenv("AGENT_USER_MODELS_FILE", str(USER_MODELS_PATH))).expanduser()
    return configured, user


def provider_endpoints() -> tuple[ProviderEndpoint, ...]:
    raw_providers: dict[str, dict[str, Any]] = {}
    configured, user = _configured_paths()
    for path in (configured, user):
        if path == user and user.resolve() == configured.resolve():
            continue
        payload = _load_yaml(path)
        providers = payload.get("providers", {})
        if isinstance(providers, dict):
            raw_providers.update({
                str(name): data for name, data in providers.items() if isinstance(data, dict)
            })
    endpoints = []
    for provider_id, raw in raw_providers.items():
        if raw.get("enabled", True) is False:
            continue
        profile = _profile_from_data(f"provider:{provider_id}", raw)
        endpoints.append(ProviderEndpoint(
            id=provider_id,
            label=str(raw.get("label", provider_id.upper())),
            discovery_timeout=max(0.5, float(raw.get("discovery_timeout", 4.0))),
            profile=replace(
                profile,
                catalog_provider=provider_id,
                provider_label=str(raw.get("label", provider_id.upper())),
            ),
        ))
    local_omlx = local_omlx_provider_spec()
    if local_omlx is not None and local_omlx.provider_id not in raw_providers:
        endpoints.append(ProviderEndpoint(
            id=local_omlx.provider_id,
            label=local_omlx.label,
            profile=local_omlx.profile,
        ))
    # Existing cloud profiles become discoverable when their credential is present.
    # Keep explicit providers (including disabled ones) authoritative.
    for profile in model_profiles().values():
        provider_id = profile.catalog_provider
        if provider_id in {"configured", "local"} or provider_id in raw_providers:
            continue
        if not profile.api_key() or any(e.id == provider_id for e in endpoints):
            continue
        generic = ModelProfile(profile.base_url, 32_768, api_key_env=profile.api_key_env,
                               api_key_resolver=profile.api_key_resolver,
                               capabilities=frozenset({"streaming"}),
                               catalog_provider=provider_id, provider_label=profile.provider_label)
        route = next((r for r in ROUTES if r.base_url == profile.base_url.rstrip("/")), None)
        label = f"{route.provider} · {route.label}" if route else profile.provider_label
        endpoints.append(ProviderEndpoint(provider_id, label, generic, inferred=True))
    for provider_id, record in read_connections().items():
        try:
            profile = record_profile(provider_id, record)
        except (KeyError, ValueError, TypeError):
            logging.getLogger(__name__).warning("Ignoring invalid provider connection: %s", provider_id)
            continue
        endpoints = [e for e in endpoints if e.id != provider_id]
        endpoints.append(ProviderEndpoint(provider_id, profile.provider_label, profile, inferred=True))
    return tuple(endpoints)


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def model_context_limit(item: dict[str, Any]) -> int | None:
    keys = (
        "context_length", "max_context_length", "max_model_len",
        "max_sequence_length", "max_position_embeddings", "n_ctx",
    )
    stack = [item]
    while stack:
        current = stack.pop()
        if not isinstance(current, dict):
            continue
        for key in keys:
            value = _positive_int(current.get(key))
            if value:
                return value
        for nested_key in ("metadata", "meta", "config", "parameters", "model_config", "details"):
            nested = current.get(nested_key)
            if isinstance(nested, dict):
                stack.append(nested)
    return None


def _model_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        data = payload.get("models")
    return [item for item in (data or []) if isinstance(item, dict)]


def entries_from_items(endpoint: ProviderEndpoint, items: list[dict], *, source: str,
                       fetched_at: float | None = None) -> list[CatalogEntry]:
    static = model_profiles()
    entries = {}
    for item in items:
        model_id = canonical_deepseek_model(str(item.get("id", "")), endpoint.profile.base_url)
        if not model_id:
            continue
        override = next((p for name, p in static.items() if (p.model_id or name) == model_id
                         and p.base_url.rstrip("/") == endpoint.profile.base_url.rstrip("/")), None)
        template = override or endpoint.profile
        context = model_context_limit(item) or template.context_limit
        output = _positive_int(item.get("max_completion_tokens")) or template.max_tokens
        capabilities = set(template.capabilities)
        if override is None:
            if isinstance(item.get("supported_parameters"), list):
                capabilities.discard("tools")
                if "tools" in item["supported_parameters"]:
                    capabilities.add("tools")
                if "reasoning" in item["supported_parameters"]:
                    capabilities.add("reasoning")
            if "image" in item.get("input_modalities", []):
                capabilities.add("vision")
        profile = replace(template, base_url=endpoint.profile.base_url,
                          api_key_env=endpoint.profile.api_key_env,
                          api_key_resolver=endpoint.profile.api_key_resolver,
                          model_id=model_id, context_limit=context, max_tokens=min(output, max(1, context // 2)),
                          reasoning_levels=tuple(item.get("reasoning_levels") or template.reasoning_levels),
                          capabilities=frozenset(capabilities), catalog_provider=endpoint.id,
                          provider_label=endpoint.label)
        entries[model_id] = CatalogEntry(f"{endpoint.id}::{model_id}", model_id, endpoint.id,
                                        endpoint.label, endpoint.profile.base_url, profile, source,
                                        override is not None or not endpoint.inferred or
                                        bool(item.get("supported_parameters")), fetched_at)
    return list(entries.values())


def normalize_model_items(payload: Any) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data", payload.get("models")), list):
        raise ValueError("Response is not a model list")
    items = _model_items(payload)
    if len(items) > model_cache.MAX_MODELS:
        raise ValueError("Model list exceeds supported size")
    normalized = {}
    for item in items:
        model_id = str(item.get("id") or item.get("name") or item.get("model") or "").strip()
        if not model_id or len(model_id) > 512 or any(ord(c) < 32 or ord(c) == 127 for c in model_id):
            continue
        top = item.get("top_provider") or {}
        architecture = item.get("architecture") or {}
        if not isinstance(top, dict) or not isinstance(architecture, dict):
            continue
        output_modalities = architecture.get("output_modalities")
        # The chat adapter cannot use an image/audio/embedding-only model.
        if isinstance(output_modalities, list) and "text" not in output_modalities:
            continue
        entry: dict[str, Any] = {"id": model_id}
        context = model_context_limit(item) or model_context_limit(top)
        if context:
            entry["context_length"] = context
        output = _positive_int(top.get("max_completion_tokens") or item.get("max_completion_tokens"))
        if output:
            entry["max_completion_tokens"] = output
        for key, values in (("supported_parameters", item.get("supported_parameters")),
                            ("input_modalities", architecture.get("input_modalities"))):
            if isinstance(values, list):
                entry[key] = [v for v in values if isinstance(v, str) and len(v) < 64][:32]
        normalized[model_id] = entry
    return list(normalized.values())


async def discover_endpoint(endpoint: ProviderEndpoint, *, force: bool = False,
                            timeout: float = 4.0) -> ModelCatalog:
    from agent.runtime.codex_auth import CodexAuthError
    cached = model_cache.read_cache(endpoint)
    fresh = model_cache.read_cache(endpoint, fresh=True)
    if fresh is not None and not force:
        return ModelCatalog(tuple(entries_from_items(endpoint, fresh[0], source="cache", fetched_at=fresh[1])),
                            {}, {endpoint.id: "cache"})
    key = endpoint.profile.api_key()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    try:
        async with asyncio.timeout(timeout):
            if endpoint.profile.provider == "openai-codex":
                from agent.runtime.codex_auth import fetch_models
                items = await fetch_models()
            else:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    response = await client.get(endpoint.profile.base_url.rstrip("/") + "/models", headers=headers)
                    response.raise_for_status()
                    items = normalize_model_items(response.json())
        try:
            stamp = model_cache.write_cache(endpoint, items)
        except OSError:
            stamp = None  # Read-only cache storage must not hide a successful live list.
        return ModelCatalog(tuple(entries_from_items(endpoint, items, source="live", fetched_at=stamp)),
                            {}, {endpoint.id: "live"})
    except CodexAuthError as exc:
        error_code, reason = "auth_failed", str(exc)
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        error_code = "listing_unsupported" if code in {404, 405} else "auth_failed" if code in {401, 403} else "http_error"
        reason = ("Authentication rejected; check this route's API key" if code in {401, 403}
                  else "Model listing is unsupported; enter a model ID manually" if code in {404, 405}
                  else f"Model listing failed (HTTP {code})")
    except (httpx.TimeoutException, TimeoutError):
        error_code = "timeout"
        reason = "Model listing timed out"
    except (httpx.HTTPError, ValueError):
        error_code = "invalid_response"
        reason = "Model listing failed or returned an invalid response"
    items = [{"id": p.model_id or n} for n, p in model_profiles().items()
             if p.base_url.rstrip("/") == endpoint.profile.base_url.rstrip("/")]
    entries = {e.key: e for e in entries_from_items(endpoint, items, source="preset")}
    if cached is not None:
        for entry in entries_from_items(endpoint, cached[0], source="stale-cache", fetched_at=cached[1]):
            entries.setdefault(entry.key, entry)
    source = "stale-cache" if cached is not None else "preset"
    return ModelCatalog(tuple(entries.values()), {endpoint.id: reason}, {endpoint.id: source},
                        {endpoint.id: error_code})



async def discover_model_catalog(timeout: float = 4.0, *, provider_id: str | None = None,
                                 force: bool = True) -> ModelCatalog:
    endpoints = [e for e in provider_endpoints() if provider_id is None or e.id == provider_id]
    groups = await asyncio.gather(*(discover_endpoint(e, force=force, timeout=timeout) for e in endpoints))
    entries = [entry for group in groups for entry in group.entries]
    dynamic_urls = {e.profile.base_url.rstrip("/") for e in endpoints}
    for entry in configured_model_catalog().entries:
        if entry.base_url.rstrip("/") not in dynamic_urls and (provider_id is None or entry.provider_id == provider_id):
            entries.append(entry)
    return ModelCatalog(tuple({e.key: e for e in entries}.values()),
                        {k: v for group in groups for k, v in group.errors.items()},
                        {k: v for group in groups for k, v in group.statuses.items()},
                        {k: v for group in groups for k, v in group.error_codes.items()})


def provider_menu_items(catalog: ModelCatalog) -> list[dict]:
    def connected(profile: ModelProfile) -> bool:
        if profile.provider == "openai-codex":
            return bool(profile.api_key())
        return bool(profile.api_key()) or not profile.api_key_env or urlsplit(profile.base_url).hostname in {"localhost", "127.0.0.1", "::1"}
    endpoints = {e.id: e for e in provider_endpoints()}
    groups = {e.provider_id: {"id": e.provider_id, "label": e.provider_label,
                              "endpoint": e.base_url, "connected": connected(e.profile)} for e in catalog.entries}
    for endpoint in endpoints.values():
        groups[endpoint.id] = {"id": endpoint.id, "label": endpoint.label,
                               "endpoint": endpoint.profile.base_url,
                               "connected": connected(endpoint.profile)}
    for provider_id, group in groups.items():
        group["source"] = catalog.statuses.get(provider_id, "preset")
        group["error"] = catalog.errors.get(provider_id, "")
        group["count"] = sum(e.provider_id == provider_id for e in catalog.entries)
    return list(groups.values())
