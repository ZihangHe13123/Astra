"""When a conversation's history is compacted: one rule for the main agent and its subagents."""

from __future__ import annotations

from contextvars import ContextVar

# Set while a request is worth sending only if the provider reads its earlier part from the cache
# (an idle summary). A provider that would have to send it another way, such as one transcript,
# refuses it instead.
CACHED_PREFIX_ONLY: ContextVar[bool] = ContextVar("astra_cached_prefix_only", default=False)

# The claude CLI is started again for every request and takes each earlier turn before it asks,
# so a long conversation is slow to its first event whatever the cache holds (live 2026-11-11:
# about 5.6 s plus 0.037 s per earlier user turn; 11 to 41 s at 484 messages and 226k prompt
# tokens), and after an hour's pause all of it is read again uncached. Half of a 1M window is
# far past that, so these models compact here instead.
CLAUDE_CODE_COMPACT_TOKENS = 150_000
# The CLI's requests are kept cached for an hour (live 2026-11-11: 97% of the input came from the
# cache after pauses of 5 to 60 minutes, none of it after longer ones). A request that changes an
# earlier message is read again from there on at the full price, the cache breakpoint being the
# newest turns: 13 requests that cleared 28 old tool results, about 47k tokens, read 1.52M tokens
# uncached to do it.
CLAUDE_CODE_CACHE_SECONDS = 3600
# How far past its compaction line a request inside a turn may go, within the model's window, before
# the history is rewritten (AgentContext.compaction_threshold), when a compaction would take off less
# than WORTHWHILE_GAIN of the line. Live 2026-11-13: a Claude Code turn reached 150k three times in
# five minutes and rewrote its history each time, reading 249k tokens uncached for 10-20k it took off.
# A compaction that takes off more is done at the line: carried to a third further, that part is
# read again in every request of the turn (a scripted turn over 125k of old notes read 69% more).
# Both are starting points from those two cases, to be checked against live compaction counts and
# uncached reads, not measured optima.
TURN_OVERSHOOT = 1 / 3
WORTHWHILE_GAIN = 1 / 4


def prompt_cache_seconds(provider: str) -> int | None:
    """How long a provider keeps a request cached, where a change to an earlier message is paid for
    by reading everything after it again. There, old tool results are cleared only in a request that
    begins after the cache has gone. None for the others, which clear them as they come due."""
    return CLAUDE_CODE_CACHE_SECONDS if provider == "claude-code" else None


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
    completion_safe = completion_safe_limit(limit, max_tokens)
    if auto_compact_token_limit is not None:
        return min(completion_safe, max(1, int(auto_compact_token_limit)))
    model_id = model.strip().lower().rsplit("::", 1)[-1].rsplit("/", 1)[-1]
    if provider == "openai-codex" or model_id.startswith(("gpt-", "chatgpt-")):
        return completion_safe
    compression_threshold = max(1, limit // 2)
    if provider == "claude-code":
        compression_threshold = min(compression_threshold, CLAUDE_CODE_COMPACT_TOKENS)
    return min(completion_safe, compression_threshold)


def completion_safe_limit(context_limit: int, max_tokens: int) -> int:
    """The largest prompt the model's window takes with room left for its answer.

    The configured completion allowance is kept (8K at least) and scaled down only when it
    would consume the entire window.
    """
    limit = max(2, int(context_limit))
    desired_reserve = max(8_192, int(max_tokens))
    reserve = desired_reserve if desired_reserve < limit else max(1, limit // 2)
    return max(1, limit - reserve)
