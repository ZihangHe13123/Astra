"""Provider connection presets and installation-private credential storage."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from agent.runtime.paths import state_path
from .models import ModelProfile


@dataclass(frozen=True)
class ConnectionRoute:
    id: str
    provider: str
    label: str
    base_url: str
    api_key_env: str
    auth_mode: str = "api-key"


# Routes are separate connections: a plan key is never retried on a billable API.
ROUTES = (
    ConnectionRoute("codex", "ChatGPT / Codex", "Subscription · sign in with ChatGPT",
                    "https://chatgpt.com/backend-api/codex", "", "oauth"),
    # The official claude CLI signs its own requests; Astra never sees the Claude login.
    ConnectionRoute("claude-code", "Claude / Claude Code", "Subscription · your signed-in claude CLI",
                    "claude-code://local", "", "oauth"),
    ConnectionRoute("deepseek", "DeepSeek", "API", "https://api.deepseek.com", "DEEPSEEK_API_KEY"),
    ConnectionRoute("qwen38", "Qwen / Model Studio", "Token Plan · Beijing", "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1", "QWEN38_API_KEY"),
    ConnectionRoute("qwen-token-sg", "Qwen / Model Studio", "Token Plan · Singapore", "https://token-plan.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1", "QWEN_TOKEN_SG_API_KEY"),
    ConnectionRoute("qwen-api", "Qwen / Model Studio", "Regular API · enter workspace / region URL", "", "DASHSCOPE_API_KEY"),
    ConnectionRoute("zhipu", "Zhipu", "API", "https://open.bigmodel.cn/api/paas/v4", "ZHIPU_API_KEY"),
    ConnectionRoute("zhipu-coding", "Zhipu", "Coding Plan", "https://open.bigmodel.cn/api/coding/paas/v4", "ZHIPU_CODING_API_KEY"),
    ConnectionRoute("hunyuan", "Hunyuan", "TokenHub", "https://tokenhub.tencentmaas.com/v1", "HUNYUAN_API_KEY"),
    ConnectionRoute("openrouter", "OpenRouter", "API", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    ConnectionRoute("custom", "Custom OpenAI-compatible", "Enter base URL", "", "LLM_API_KEY"),
)


def private_json_write(path: Path, data: dict) -> None:
    """Replace atomically, with owner-only POSIX permissions, including the temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def connections_path() -> Path:
    override = os.getenv("AGENT_CONNECTIONS_DIR", "").strip()
    return Path(override).expanduser() if override else state_path("connections")


def read_connections() -> dict[str, dict]:
    records = {}
    for path in sorted(connections_path().glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") != 1 or not isinstance(data.get("connection"), dict):
                raise ValueError
            records[path.stem] = data["connection"]
        except (OSError, ValueError, AttributeError, UnicodeError):
            logging.getLogger(__name__).warning("Ignoring unreadable provider connection file: %s", path.name)
    return records


def validate_base_url(value: str) -> str:
    if value.strip() == "claude-code://local":
        return "claude-code://local"
    url = value.strip().rstrip("/")
    parsed = urlsplit(url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in url) or "{" in url or "}" in url):
        raise ValueError("Use an HTTP(S) API base URL without credentials, query or placeholders.")
    if url.endswith(("/models", "/chat/completions")):
        raise ValueError("Use the API base URL, without /models or /chat/completions.")
    return url


def connection_record(route_id: str, *, base_url: str = "", api_key: str = "", api_key_env: str = "") -> tuple[str, dict]:
    route = next((r for r in ROUTES if r.id == route_id), None)
    if route is None:
        raise ValueError("Unknown provider route. Reopen /connect.")
    url = validate_base_url(base_url or route.base_url)
    if route.base_url and url != route.base_url:
        raise ValueError("Use Custom OpenAI-compatible for a different endpoint.")
    if route.auth_mode == "oauth":
        if api_key or api_key_env:
            raise ValueError(f"{route.provider} uses its own sign-in, not an API key.")
        return route.id, {"label": f"{route.provider} · {route.label}", "base_url": url,
                         "route_id": route.id, "auth_mode": "oauth"}
    if api_key_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env):
        raise ValueError("Enter an environment variable name, not its value.")
    if any(ord(c) < 32 or ord(c) == 127 for c in api_key) or len(api_key) > 8192:
        raise ValueError("Invalid API key.")
    if api_key and api_key_env:
        raise ValueError("Choose an API key or an environment reference.")
    if api_key_env and not os.getenv(api_key_env, "").strip():
        raise ValueError(f"Environment variable {api_key_env} is empty in this process.")
    if not api_key and not api_key_env and urlsplit(url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Enter an API key or use an existing environment variable.")
    provider_id = route.id if route.base_url else f"{route.id}-{hashlib.sha256(url.encode()).hexdigest()[:10]}"
    return provider_id, {"label": f"{route.provider} · {route.label}", "base_url": url,
                         "api_key": api_key.strip(), "api_key_env": api_key_env, "route_id": route.id}


def record_profile(provider_id: str, record: dict) -> ModelProfile:
    if record.get("route_id") == "claude-code":
        from agent.runtime.claude_code_provider import BASE_URL, claude_command
        return ModelProfile(base_url=BASE_URL, context_limit=200_000, provider="claude-code",
            api_key_env="", api_key_resolver=lambda: "claude-cli" if claude_command() else "",
            catalog_provider=provider_id, provider_label=str(record["label"]),
            capabilities=frozenset({"streaming", "tools", "reasoning", "vision"}),
            reasoning_levels=("low", "medium", "high", "xhigh", "max"))
    if record.get("route_id") == "codex":
        from agent.runtime.codex_auth import BASE_URL, credential_hint
        return ModelProfile(base_url=BASE_URL, context_limit=32_768, provider="openai-codex",
            api_key_env="", api_key_resolver=credential_hint,
            catalog_provider=provider_id, provider_label=str(record["label"]),
            capabilities=frozenset({"streaming", "tools", "reasoning"}))
    return ModelProfile(
        base_url=validate_base_url(record["base_url"]), context_limit=32_768,
        api_key_env=record.get("api_key_env", ""),
        api_key_resolver=lambda: str(record.get("api_key", "")),
        catalog_provider=provider_id, provider_label=str(record["label"]),
        capabilities=frozenset({"streaming"}),
    )


def save_connection(provider_id: str, record: dict) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", provider_id):
        raise ValueError("Invalid provider ID.")
    # Independent files prevent two processes saving different connections from
    # overwriting one another. Updating the same connection is last-writer-wins.
    private_json_write(connections_path() / f"{provider_id}.json",
                       {"version": 1, "connection": record})


def connection_routes_event() -> list[dict]:
    return [{**asdict(route), "key_available": bool(os.getenv(route.api_key_env, "").strip())}
            for route in ROUTES]
