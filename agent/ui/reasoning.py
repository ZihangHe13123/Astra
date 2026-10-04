"""Read-only reasoning text for chat presentation, separate from provider input."""

from __future__ import annotations


def reasoning_display_fields(message: dict) -> dict:
    """Copy only the saved assistant text; opaque provider state stays private."""
    text = message.get("reasoning_content")
    if message.get("role") == "assistant" and isinstance(text, str) and text.strip():
        return {"reasoning_content": text}
    return {}


def legacy_reasoning_text(message: dict) -> str | None:
    """Present a retired sidecar at its original position without migrating it."""
    meta = message.get("_meta")
    if (message.get("role") != "user" or not isinstance(meta, dict)
            or meta.get("type") != "reasoning_context"):
        return None
    text = message.get("content")
    if not isinstance(text, str):
        return None
    text = text.split("\n\n", 1)[-1].strip()
    return text or None
