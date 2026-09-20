"""Provider-agnostic token estimation helpers."""

import json
import math
import re


_IMAGE_TOKEN_BUDGETS = {
    "low": 512,
    "auto": 2_048,
    "high": 4_096,
    "original": 4_096,
}


def _image_part_tokens(value: dict) -> int | None:
    """Estimate multimodal image parts without counting data-URL bytes as text.

    OpenAI-compatible vision servers decode the image before tokenization. A
    multi-megabyte base64 string therefore does not consume one text token per
    few characters. Exact visual tokenization is model-specific, so use a
    conservative fixed allowance keyed by the requested detail level.
    """
    if value.get("type") != "image_url":
        return None
    image = value.get("image_url")
    if not isinstance(image, dict):
        image = value.get("data")
    detail = str(image.get("detail", "auto")) if isinstance(image, dict) else "auto"
    return _IMAGE_TOKEN_BUDGETS.get(detail, _IMAGE_TOKEN_BUDGETS["auto"])


def estimate_value_tokens(value) -> int:
    """Cheap fallback estimate for prompt budgeting.

    It is intentionally conservative for CJK and structured data, and cheaper
    for long base64-like blobs that would otherwise dominate context estimates.
    """
    if value is None:
        return 1
    if isinstance(value, str):
        if not value:
            return 1
        base64_chars = sum(len(match.group(0)) for match in re.finditer(r"[A-Za-z0-9+/=]{80,}", value))
        cjk_chars = sum(1 for ch in value if "\u4e00" <= ch <= "\u9fff")
        structural_chars = sum(1 for ch in value if ch in "{}[],:\"")
        other_chars = max(0, len(value) - base64_chars - cjk_chars)
        estimate = (
            cjk_chars
            + math.ceil(other_chars / 4)
            + math.ceil(structural_chars / 8)
            + math.ceil(base64_chars / 8)
        )
        return max(1, estimate)
    if isinstance(value, (int, float, bool)):
        return 1
    if isinstance(value, dict):
        if "_provider_state" in value and value.get("role") == "assistant":
            state = value["_provider_state"]
            hidden = state.get("reasoning_tokens", 0) if isinstance(state, dict) else 0
            # Ciphertext bytes are not text tokens. Use the server's reasoning
            # token count when available, so session budgets remain meaningful.
            return estimate_value_tokens({k: v for k, v in value.items() if k != "_provider_state"}) + (
                max(0, hidden) if isinstance(hidden, int) else 0)
        image_tokens = _image_part_tokens(value)
        if image_tokens is not None:
            return image_tokens
        return max(1, sum(estimate_value_tokens(k) + estimate_value_tokens(v) for k, v in value.items()))
    if isinstance(value, list):
        return max(1, sum(estimate_value_tokens(item) for item in value))
    return estimate_value_tokens(str(value))


def estimate_messages_tokens(messages: list[dict]) -> int:
    return estimate_value_tokens(messages)


def messages_to_tokenize_text(messages: list[dict]) -> str:
    return json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
