"""Configuration-backed model profiles shared by all frontends."""

from __future__ import annotations

from agent.runtime.paths import state_path

import os
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from agent.runtime.deepseek import DEEPSEEK_FLASH, DEEPSEEK_VISION_MODELS, canonical_deepseek_model
from agent.runtime.vision_policy import VisionPreprocessPolicy, parse_vision_preprocess_policy

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - dependency error is reported on use
    yaml = None


DEFAULT_CONTEXT_LIMIT = 128_000
VISION_PREPROCESS_MODEL = DEEPSEEK_FLASH
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MODELS_PATH = PROJECT_ROOT / "config" / "models.yaml"
PACKAGED_MODELS_PATH = Path(__file__).resolve().parents[1] / "_data" / "models.yaml"
USER_MODELS_PATH = state_path("models.yaml", root=PROJECT_ROOT)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModelProfile:
    base_url: str
    context_limit: int
    temperature: float | None = None
    max_tokens: int = 4096
    top_p: float | None = None
    top_k: int | None = None
    min_p: float | None = None
    presence_penalty: float | None = None
    repetition_penalty: float | None = None
    repetition_penalty_parameter: str | None = None
    provider: str = "openai-compatible"
    api_key_env: str = "LLM_API_KEY"
    capabilities: frozenset[str] = field(default_factory=frozenset)
    model_id: str = ""
    catalog_provider: str = "configured"
    provider_label: str = "Configured"
    vision_detail: str = "auto"
    reasoning_levels: tuple[str, ...] = ()
    vision_preprocess: VisionPreprocessPolicy | None = None
    api_key_resolver: Callable[[], str] | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def generation_settings(self) -> dict:
        settings = {
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "top_p": self.top_p,
            "top_k": self.top_k,
            "min_p": self.min_p,
            "presence_penalty": self.presence_penalty,
            "repetition_penalty": self.repetition_penalty,
        }
        if self.repetition_penalty_parameter:
            settings["repetition_penalty_parameter"] = self.repetition_penalty_parameter
        settings["vision_detail"] = self.vision_detail
        settings["vision_preprocess"] = self.vision_preprocess
        if self.provider == "openai-codex":
            settings["reasoning_levels"] = self.reasoning_levels
        return settings

    def api_key(self) -> str:
        configured = os.getenv(self.api_key_env, "") if self.api_key_env else ""
        if configured:
            return configured
        if self.api_key_resolver is None:
            return ""
        try:
            return str(self.api_key_resolver() or "").strip()
        except Exception:
            return ""


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    if yaml is None:
        raise RuntimeError("PyYAML is required to load model profiles")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid model config {path}: {exc}") from exc
    return loaded if isinstance(loaded, dict) else {}


def _profile_from_data(name: str, raw: dict[str, Any]) -> ModelProfile:
    generation = raw.get("generation", {})
    if not isinstance(generation, dict):
        raise ValueError(f"Model '{name}' generation must be an object")
    base_url = str(raw.get("base_url", "")).strip()
    env_name = str(raw.get("base_url_env", "")).strip()
    if env_name:
        # Empty optional .env entries must not erase the configured endpoint.
        base_url = os.getenv(env_name, "").strip() or base_url
    if not base_url:
        raise ValueError(f"Model '{name}' is missing base_url")
    try:
        context_limit = int(raw.get("context_limit", DEFAULT_CONTEXT_LIMIT))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Model '{name}' has invalid context_limit") from exc
    if context_limit <= 0:
        raise ValueError(f"Model '{name}' context_limit must be positive")

    capabilities = raw.get("capabilities", [])
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        raise ValueError(f"Model '{name}' capabilities must be a string list")
    vision_preprocess = parse_vision_preprocess_policy(name, raw.get("vision_preprocess"))
    if vision_preprocess is not None:
        if name not in DEEPSEEK_VISION_MODELS:
            raise ValueError(
                f"Model '{name}' vision_preprocess is only supported for "
                f"{VISION_PREPROCESS_MODEL}"
            )
        if "vision" not in capabilities:
            raise ValueError(f"Model '{name}' vision_preprocess requires the vision capability")

    model_id = str(raw.get("model_id", ""))
    migrated_id = canonical_deepseek_model(model_id or name, base_url)
    if migrated_id != (model_id or name):
        model_id = migrated_id
    return ModelProfile(
        base_url=base_url,
        context_limit=context_limit,
        provider=str(raw.get("provider", "openai-compatible")),
        api_key_env=str(raw.get("api_key_env", "LLM_API_KEY")),
        capabilities=frozenset(capabilities),
        model_id=model_id,
        catalog_provider=str(raw.get("catalog_provider", "configured")),
        provider_label=str(raw.get("provider_label", "Configured")),
        vision_detail=_vision_detail(raw.get("vision_detail", "auto")),
        vision_preprocess=vision_preprocess,
        temperature=_optional_float(generation.get("temperature")),
        max_tokens=int(generation.get("max_tokens", 4096)),
        top_p=_optional_float(generation.get("top_p")),
        top_k=_optional_int(generation.get("top_k")),
        min_p=_optional_float(generation.get("min_p")),
        presence_penalty=_optional_float(generation.get("presence_penalty")),
        repetition_penalty=_optional_float(generation.get("repetition_penalty")),
        repetition_penalty_parameter=_repetition_penalty_parameter(
            raw.get("repetition_penalty_parameter")
        ),
    )


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _repetition_penalty_parameter(value: Any) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    normalized = str(value).strip()
    if normalized not in {"repetition_penalty", "repeat_penalty"}:
        raise ValueError(
            "repetition_penalty_parameter must be repetition_penalty or repeat_penalty"
        )
    return normalized


def _vision_detail(value: Any) -> str:
    normalized = str(value or "auto").strip().lower()
    if normalized not in {"auto", "low", "high", "original"}:
        raise ValueError("vision_detail must be auto, low, high, or original")
    return normalized


def _optional_int(value: Any) -> int | None:
    return None if value is None else int(value)


def _models_from_file(path: Path) -> dict[str, dict[str, Any]]:
    payload = _load_yaml(path)
    models = payload.get("models", {})
    if not isinstance(models, dict):
        raise ValueError(f"Model config {path} must contain a 'models' object")
    return {str(name): raw for name, raw in models.items() if isinstance(raw, dict)}


def model_profiles() -> dict[str, ModelProfile]:
    """Load bundled profiles plus optional user overrides."""
    bundled = DEFAULT_MODELS_PATH if DEFAULT_MODELS_PATH.exists() else PACKAGED_MODELS_PATH
    configured = Path(os.getenv("AGENT_MODELS_FILE", str(bundled))).expanduser()
    raw_profiles = _models_from_file(configured)
    override = Path(os.getenv("AGENT_USER_MODELS_FILE", str(USER_MODELS_PATH))).expanduser()
    if override.resolve() != configured.resolve() and override.exists():
        try:
            raw_profiles.update(_models_from_file(override))
        except ValueError as exc:
            logger.warning("ignoring invalid user model config path=%s error=%s", override, exc)
    profiles = {name: _profile_from_data(name, raw) for name, raw in raw_profiles.items()}
    # Retired official user entries must not downgrade the canonical Flash
    # profile or reappear as duplicate choices. Custom endpoint IDs survive.
    return {
        name: profile for name, profile in profiles.items()
        if canonical_deepseek_model(name, profile.base_url) == name
        or canonical_deepseek_model(name, profile.base_url) not in profiles
    }


def save_model_profile(name: str, profile: ModelProfile, path: Path | None = None) -> Path:
    """Persist a user-defined connection without storing its secret value."""
    if not name.strip() or any(ch.isspace() for ch in name):
        raise ValueError("Model profile name must be non-empty and contain no spaces")
    if yaml is None:
        raise RuntimeError("PyYAML is required to save model profiles")
    target = path or Path(os.getenv("AGENT_USER_MODELS_FILE", str(USER_MODELS_PATH))).expanduser()
    payload = _load_yaml(target)
    models = payload.setdefault("models", {})
    if not isinstance(models, dict):
        raise ValueError(f"Model config {target} must contain a 'models' object")
    data = asdict(profile)
    data.pop("api_key_resolver", None)
    data["capabilities"] = sorted(profile.capabilities)
    generation = {
        key: data.pop(key)
        for key in (
            "temperature", "max_tokens", "top_p", "top_k", "min_p",
            "presence_penalty", "repetition_penalty",
        )
        if data.get(key) is not None
    }
    data["generation"] = generation
    models[name] = data
    payload.setdefault("version", 1)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False), encoding="utf-8")
    temporary.replace(target)
    return target


def context_limit_for_model(model: str) -> int:
    profiles = model_profiles()
    profile = profiles.get(resolve_profile_key(model, profiles))
    return profile.context_limit if profile else DEFAULT_CONTEXT_LIMIT


def resolve_profile_key(value: str, profiles: dict[str, ModelProfile]) -> str:
    """Accept saved official V4 selectors while retaining custom profile keys."""
    if value in profiles:
        return value
    provider_id, _, model_id = value.rpartition("::")
    model_id = model_id or value
    eligible = {
        name: profile for name, profile in profiles.items()
        if not provider_id or provider_id in {"configured", profile.catalog_provider}
    }
    exact = [name for name, profile in eligible.items() if (profile.model_id or name) == model_id]
    if exact:
        return exact[0] if len(exact) == 1 else value
    matches = []
    for name, profile in eligible.items():
        migrated = canonical_deepseek_model(model_id, profile.base_url)
        if migrated == (profile.model_id or name):
            if name == migrated:
                return name
            matches.append(name)
    return matches[0] if len(matches) == 1 else value


def prompt_token_budget(context_limit: int, max_tokens: int) -> int:
    """Return the token threshold at which proactive compression begins.

    Proactive compression starts at 50% of the model context window (aligned
    with Hermes' compression.threshold=0.5), so long contexts never ride into
    the degraded-attention zone before compacting. Small windows may need an
    earlier threshold to preserve the configured completion allowance (with an
    8K minimum), so the stricter of the two limits wins.
    """
    limit = max(2, int(context_limit))
    desired_reserve = max(8_192, int(max_tokens))
    reserve = desired_reserve if desired_reserve < limit else max(1, limit // 2)
    completion_safe_limit = max(1, limit - reserve)
    compression_threshold = max(1, limit // 2)
    return min(completion_safe_limit, compression_threshold)
