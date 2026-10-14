"""When a conversation's history is compacted: one rule for the main agent and its subagents."""

from __future__ import annotations


def prompt_token_budget(context_limit: int, max_tokens: int, *, model: str = "", provider: str = "",
                        auto_compact_token_limit: int | None = None) -> int:
    """Return the token threshold at which proactive compression begins.

    GPT models and the Codex provider use the model window minus the output
    reserve, without the additional 50% proactive threshold used by other
    models. Preserve the configured completion allowance (with an 8K minimum),
    scaling the reserve down only when it would consume the entire window.
    """
    limit = max(2, int(context_limit))
    desired_reserve = max(8_192, int(max_tokens))
    reserve = desired_reserve if desired_reserve < limit else max(1, limit // 2)
    completion_safe_limit = max(1, limit - reserve)
    if auto_compact_token_limit is not None:
        return min(completion_safe_limit, max(1, int(auto_compact_token_limit)))
    model_id = model.strip().lower().rsplit("::", 1)[-1].rsplit("/", 1)[-1]
    if provider == "openai-codex" or model_id.startswith(("gpt-", "chatgpt-")):
        return completion_safe_limit
    compression_threshold = max(1, limit // 2)
    return min(completion_safe_limit, compression_threshold)
