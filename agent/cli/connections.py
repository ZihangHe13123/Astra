"""Model connection creation, probing, and atomic switching."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from .model_preferences import save_selected_model
from .models import ModelProfile, prompt_token_budget, save_model_profile


@dataclass(frozen=True)
class ConnectionProbe:
    ok: bool
    message: str
    models: tuple[str, ...] = ()


def is_local_url(url: str) -> bool:
    return urlparse(url).hostname in {"localhost", "127.0.0.1", "::1"}


async def probe_profile(
    name: str,
    profile: ModelProfile,
    timeout: float = 5.0,
    *,
    api_key: str | None = None,
    base_url: str | None = None,
) -> ConnectionProbe:
    if profile.provider == "claude-code":
        from agent.runtime.claude_code_provider import MODELS, login_status
        ready, message = await login_status(timeout=timeout)
        return ConnectionProbe(ready, message, MODELS if ready else ())
    if profile.provider == "openai-codex":
        from agent.runtime.codex_auth import fetch_models
        try:
            models = await fetch_models()
            return ConnectionProbe(True, "Connected to ChatGPT / Codex", tuple(m["id"] for m in models))
        except (ValueError, httpx.HTTPError) as exc:
            return ConnectionProbe(False, str(exc))
    resolved_key = profile.api_key() if api_key is None else api_key
    resolved_url = base_url or profile.base_url
    if not resolved_key and not is_local_url(resolved_url):
        return ConnectionProbe(False, f"Missing API key environment variable: {profile.api_key_env}")
    headers = {"Authorization": f"Bearer {resolved_key}"} if resolved_key else {}
    url = resolved_url.rstrip("/") + "/models"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return ConnectionProbe(False, f"{type(exc).__name__}: {exc}")
    data = payload.get("data", []) if isinstance(payload, dict) else []
    models = tuple(
        str(item.get("id") or item.get("name"))
        for item in data
        if isinstance(item, dict) and (item.get("id") or item.get("name"))
    )
    visible = ", ".join(models[:5]) if models else "endpoint returned no model ids"
    return ConnectionProbe(True, f"Connected to {name} @ {resolved_url}; {visible}", models)


def sync_context_budget(agent, profile: ModelProfile) -> int:
    """Apply a model profile's current context metadata to a live agent."""
    budget = prompt_token_budget(
        profile.context_limit, agent.llm.config.max_tokens,
        model=profile.model_id or getattr(agent.llm.config, "model", ""), provider=profile.provider,
    )
    agent.context.max_prompt_tokens = budget
    agent.llm.config.context_limit = profile.context_limit
    return budget


def switch_to_profile(agent, name: str, profile: ModelProfile, *, valid_models: set[str] | None = None):
    """Build the new provider first, then commit config and context changes."""
    api_key = profile.api_key()
    if not api_key:
        if is_local_url(profile.base_url) or not profile.api_key_env:
            api_key = "local"
        else:
            raise ValueError(
                f"Missing API key environment variable: {profile.api_key_env}"
            )
    runtime_model = profile.model_id or name
    previous_config, previous_provider = agent.llm.config, agent.llm.provider
    previous_budget = agent.context.max_prompt_tokens
    try:
        generation = profile.generation_settings()
        generation["repetition_penalty_parameter"] = profile.repetition_penalty_parameter
        agent.llm.switch_model(
            runtime_model, profile.base_url, provider_name=profile.provider, api_key=api_key,
            capabilities=profile.capabilities, **generation,
        )
        sync_context_budget(agent, profile)
        return save_selected_model(name, valid_models=valid_models)
    except Exception:
        agent.llm.config, agent.llm.provider = previous_config, previous_provider
        agent.context.max_prompt_tokens = previous_budget
        raise



async def create_probe_and_switch(
    agent,
    name: str,
    base_url: str,
    api_key_env: str = "LLM_API_KEY",
    provider: str = "openai-compatible",
) -> tuple[ConnectionProbe, object | None]:
    profile = ModelProfile(
        base_url=base_url,
        context_limit=128_000,
        provider=provider,
        api_key_env=api_key_env,
        capabilities=frozenset({"tools", "streaming"}),
    )
    probe = await probe_profile(name, profile)
    if not probe.ok:
        return probe, None
    save_model_profile(name, profile)
    settings_path = switch_to_profile(agent, name, profile)
    return probe, settings_path


async def connect_provider(route_id: str, *, base_url: str = "", api_key: str = "", api_key_env: str = "", on_progress=None):
    """Validate/discover/save a connection without changing the current model."""
    from .provider_connections import connection_record, record_profile, save_connection
    from .model_catalog import ProviderEndpoint, discover_endpoint
    provider_id, record = connection_record(route_id, base_url=base_url, api_key=api_key, api_key_env=api_key_env)
    if route_id == "codex":
        from agent.runtime.codex_auth import credential_hint, device_login
        if not credential_hint():
            if on_progress is None:
                raise ValueError("Sign in first with astra auth login.")
            await device_login(on_progress)
    if route_id == "claude-code":
        from agent.runtime.claude_code_provider import login_status
        ready, message = await login_status()
        if not ready:
            raise ValueError(message)
    profile = record_profile(provider_id, record)
    catalog = await discover_endpoint(ProviderEndpoint(provider_id, profile.provider_label, profile, inferred=True),
                                      force=True, timeout=30 if route_id == "codex" else 4)
    if provider_id in catalog.errors and catalog.error_codes.get(provider_id) != "listing_unsupported":
        raise ValueError(catalog.errors[provider_id])
    save_connection(provider_id, record)
    return provider_id, catalog


async def prompt_provider_connection(agent) -> None:
    """Plain CLI counterpart of the Ink connection panel; secrets use getpass."""
    import asyncio
    import getpass
    from .provider_connections import ROUTES
    from .model_catalog import configured_model_catalog

    async def choose(labels: list[str], prompt: str) -> int:
        for i, label in enumerate(labels, 1):
            print(f"  {i}. {label}")
        raw = await asyncio.to_thread(input, prompt + " (number, empty cancels): ")
        if not raw:
            raise EOFError
        selected = int(raw) - 1
        if not 0 <= selected < len(labels):
            raise ValueError("Invalid selection.")
        return selected

    try:
        providers = list(dict.fromkeys(route.provider for route in ROUTES))
        provider = providers[await choose(providers, "Provider")]
        routes = [route for route in ROUTES if route.provider == provider]
        route = routes[await choose([route.label for route in routes], "API route")]
        base_url = route.base_url or await asyncio.to_thread(input, "API base URL: ")
        key = env = ""
        if route.auth_mode != "oauth":
            key = await asyncio.to_thread(getpass.getpass, "API key (hidden; empty uses an environment variable): ")
            env = "" if key else await asyncio.to_thread(input, f"Environment variable [{route.api_key_env}]: ")
            env = env or (route.api_key_env if not key else "")
        async def progress(challenge):
            print("Enable Codex device-code login in ChatGPT Settings → Security if authorization is disabled.")
            print(f"Open {challenge['verification_uri']} and enter {challenge['user_code']}", flush=True)
        provider_id, catalog = await connect_provider(route.id, base_url=base_url, api_key=key, api_key_env=env,
                                                      on_progress=progress)
        key = ""
        for error in catalog.errors.values():
            print(f"  {error}")
        query = await asyncio.to_thread(input, "Search models (empty shows the first 20): ")
        matching = [entry for entry in catalog.entries if query.lower() in entry.model_id.lower()]
        for entry in matching[:20]:
            print(f"  {entry.model_id} · {entry.source}")
        name = await asyncio.to_thread(input, "Model ID to use (empty keeps current): ")
        if not name.strip():
            return
        selector = f"{provider_id}::{name.strip()}"
        entry = catalog.resolve(selector) or configured_model_catalog().resolve_persisted(selector)
        if entry is None:
            raise ValueError("Invalid model ID.")
        switch_to_profile(agent, entry.key, entry.profile, valid_models={entry.key})
        print(f"  Selected {entry.key}; saved as startup default.")
    except (EOFError, KeyboardInterrupt):
        print("  Connection setup closed.")
    except (ValueError, OSError) as exc:
        print(f"  Connection setup failed: {exc}")
