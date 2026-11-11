"""When a conversation's history is compacted: one rule for the main agent and its subagents."""

from __future__ import annotations

# The claude CLI is started again for every request and takes each earlier turn before it asks,
# so a long conversation is slow to its first event whatever the cache holds (live 2026-11-11:
# about 5.6 s plus 0.037 s per earlier user turn; 11 to 41 s at 484 messages and 226k prompt
# tokens), and after an hour's pause all of it is read again uncached. Half of a 1M window is
# far past that, so these models compact here instead.
CLAUDE_CODE_COMPACT_TOKENS = 150_000


def prompt_token_budget(context_limit: int, max_tokens: int, *, model: str = "", provider: str = "",
                        auto_compact_token_limit: int | None = None) -> int:
    """Return the token threshold at which proactive compression begins.

    GPT models and the Codex provider use the model window minus the output
    reserve, without the additional 50% proactive threshold used by other
    models. Claude Code models stop at CLAUDE_CODE_COMPACT_TOKENS when half their
    window is more. A model's own auto_compact_token_limit replaces all three rules.
    Preserve the configured completion allowance (with an 8K minimum),
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
    if provider == "claude-code":
        compression_threshold = min(compression_threshold, CLAUDE_CODE_COMPACT_TOKENS)
    return min(completion_safe_limit, compression_threshold)
