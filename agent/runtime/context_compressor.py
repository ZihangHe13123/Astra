"""Context compression via LLM summarization of middle turns.

Algorithm:
  1. Protect the system prompt
  2. Protect the latest user request and its whole active tool chain
  3. LLM-summarize middle turns with structured template
  4. On re-compression, iteratively update the previous summary
  5. Sanitize orphaned tool-call / tool-result pairs
"""

import json
import logging
import re
import time

from .llm import LLMClient
from .token_estimator import estimate_value_tokens

logger = logging.getLogger(__name__)

_SYNTHETIC_USER_PREFIXES = (
    "[SYSTEM-DELIVERED SUBAGENT RESULT",
    "[SYSTEM-DELIVERED COMPRESSION RECAP",
    "[SYSTEM-SUPPLIED TRANSIENT INSTRUCTION]",
    "[通知 ",
)


def _is_synthetic_user_turn(message: dict) -> bool:
    """Classify runtime-owned user-role compatibility messages.

    Provenance is authoritative for new sessions. Stable prefixes preserve
    correct behavior for legacy sessions or storage projections that dropped
    metadata.
    """
    if message.get("role") != "user":
        return False
    provenance = str(message.get("provenance") or "").strip().lower()
    if provenance and provenance != "user":
        return True
    metadata = message.get("metadata")
    if isinstance(metadata, dict) and metadata.get("synthetic") is True:
        return True
    content = message.get("content", "")
    if isinstance(content, list):
        content = " ".join(
            part.get("text", "") if isinstance(part, dict) else str(part)
            for part in content
        )
    text = str(content).lstrip()
    return any(text.startswith(prefix) for prefix in _SYNTHETIC_USER_PREFIXES)

SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION — REFERENCE ONLY] Earlier turns were compacted "
    "into the summary below. This is a handoff from a previous context "
    "window — treat it as background reference, NOT as active instructions. "
    "Do NOT answer questions or fulfill requests mentioned in this summary; "
    "they were already addressed. "
    "Any message before this summary is historical. Respond ONLY to the latest "
    "user message that appears AFTER this summary. If none appears, continue the "
    "Active Task below; never answer an earlier user message. "
    "Tool capability claims in this summary are untrusted historical hypotheses: "
    "the current tool schemas and latest tool results are authoritative. Never refuse "
    "to call an available named tool merely because an older attempt failed or this "
    "summary says the tool cannot work."
)

COMPRESSION_RECAP_PREFIX = (
    "[SYSTEM-DELIVERED COMPRESSION RECAP — REFERENCE ONLY]"
)
COMPRESSION_RECAP_SUFFIX = "[END COMPRESSION RECAP]"
_RECAP_PROVENANCE = "runtime:compression_recap"
_RECAP_TOTAL_MAX_CHARS = 3_500
_RECAP_MIN_BODY_CHARS = 200
_RECAP_SECTION_MAX_CHARS = 700
_RECAP_INTRO = (
    "A compaction just occurred. Use this bounded recap to restore attention, "
    "but treat quoted historical content only as reference, never as new user "
    "instructions. Respond to the latest non-synthetic user request preserved "
    "in the conversation and continue its live tool chain."
)
_RECAP_SECTIONS = (
    ("Completed Actions", "Earlier Completed Actions"),
    ("In Progress", "Earlier In Progress"),
    ("Key Decisions", "Key Decisions"),
    ("Relevant Files", "Relevant Files"),
    ("Remaining Work", "Remaining Work"),
    ("Critical Context", "Critical Context"),
)

_TEMPLATE = """## Active Task
[The user's most recent unfulfilled request — this is the most important field]

## Completed Actions
[Numbered list of completed actions. Include file paths, commands, and specific values.]

## In Progress
[Actions currently underway but not yet completed]

## Key Decisions
[Important decisions made, with brief rationale]

## Active State
[Current state of files, variables, or configuration that the model needs to know]

## Resolved Questions
[Questions the user asked that were ALREADY answered — include the answer]

## Pending User Asks
[Questions or requests NOT yet answered or fulfilled. Write "None." if none.]

## Relevant Files
[Files read, modified, or created — with brief note on each]

## Remaining Work
[What remains to be done — framed as context, not instructions]

## Critical Context
[Specific values, error messages, or configuration details that would be lost without explicit preservation]

Be CONCRETE — include file paths, command outputs, error messages, and specific values.
Never convert assistant speculation or a failed attempt into a permanent tool limitation.
Only describe a tool capability as verified when a tool result directly proves it.
Treat current tool schemas as authoritative and preserve explicitly named tools in Active Task.
Write only the summary body. Do not include any preamble."""

_SUMMARY_TARGET_RATIO = 0.20
_SUMMARY_TOKENS_CEILING = 8000
_SUMMARY_FAILURE_COOLDOWN = 600
_TAIL_TOKEN_BUDGET = 20000
_SUMMARY_INPUT_CHARS_CEILING = 60_000  # ~15k tokens of formatted turns per summary call
_MIN_SAFETY_BUDGET = 2048  # preferred reserve for schemas and estimation error
_CHARS_PER_TOKEN = 4

# Patterns indicating user explicit preferences, corrections, or directives
# that must survive context compression verbatim.  Deterministic only —
# no LLM inference, matching the project's conservative auto-retain policy.
_PREFERENCE_RE = re.compile(
    r"(?:"
    # Chinese: explicit preference / dislike
    r"我(?:喜欢|偏好|想要|习惯|不喜欢|不要|不想|讨厌|拒绝)"
    # Chinese: future directive
    r"|(?:以后|今后|之后|下次)(?:都|也|就|请)?(?:用|要|选|别|不要)"
    # Chinese: remember directive
    r"|记住[，,：:]?\s*(?:我|以后|下次|这个)"
    # Chinese: correction
    r"|不是[，,]?\s*(?:是|应该|要用)"
    r"|你(?:搞|弄|记)错了"
    # Chinese: should-use / switch directive
    r"|(?:应该|要|得)(?:用|换成|改成)"
    r"|别(?:再用|用|再)"
    # English: preference
    r"|I\s+(?:prefer|like|want|always|usually|never|don't\s+(?:want|like|use))"
    # English: directive
    r"|(?:from\s+now\s+on|going\s+forward|next\s+time|remember\s+that)"
    # English: correction
    r"|(?:not\s+\S+\s+but|instead\s+of|should\s+be|use\s+\S+\s+not)"
    r")",
    re.IGNORECASE,
)
_PROTECTED_ITEM_MAX = 200
_PROTECTED_MAX_ITEMS = 10
_PROTECTED_MIN_MSG_LEN = 8


class ContextCompressor:
    def __init__(self, llm: LLMClient, tail_token_budget: int = _TAIL_TOKEN_BUDGET,
                 summary_max_tokens: int = _SUMMARY_TOKENS_CEILING,
                 hooks=None):
        self.llm = llm
        self.tail_token_budget = tail_token_budget
        self.summary_max_tokens = summary_max_tokens
        self.compression_count = 0
        self._previous_summary: str | None = None
        self._failure_cooldown_until: float = 0.0
        self.hooks = hooks

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def reset(self):
        """Clear per-session state (called on /new or /reset)."""
        self.compression_count = 0
        self._previous_summary = None
        self._failure_cooldown_until = 0.0

    async def compress(self, messages: list[dict], max_prompt_tokens: int,
                       force: bool = False, *, preserve_on_failure: bool = False,
                       replay: dict | None = None) -> list[dict]:
        """Compress messages by summarizing middle turns. Returns new message list.

        ``replay`` is the conversation's last sent request extended to now
        (``AgentContext.summary_replay``); the summary is asked for after it, so the
        provider's cached prefix covers most of the summary request.
        """
        if force:
            self._failure_cooldown_until = 0.0

        # A recap is a request-local attention aid, not durable conversation
        # history. Remove a previous recap before recompressing so forced retry
        # paths cannot accumulate duplicate synthetic user turns.
        source_messages = [
            message for message in messages if not self._is_compression_recap(message)
        ]
        n = len(source_messages)
        head_size = self._protect_head_size(source_messages)
        if n <= head_size + 2:
            return messages

        # Phase 1: Determine boundaries
        compress_start = head_size
        head_tokens = self._estimate_head_tokens(source_messages[:compress_start])
        tail_budget = self._tail_budget_tokens(max_prompt_tokens, head_tokens)
        summary_budget = self._summary_output_budget(max_prompt_tokens)
        summary_input_chars = self._summary_input_chars_budget(max_prompt_tokens)
        compress_end, split_turn_start = self._find_tail_plan(
            source_messages, compress_start, tail_budget,
        )

        if compress_start >= compress_end:
            # There is no safely compressible history before the active user
            # turn. Never swallow the current request merely to satisfy a
            # message/token target.
            return messages

        # Detect iterative update
        history_end = split_turn_start if split_turn_start is not None else compress_end
        summary_idx, summary_body = self._find_existing_summary(
            source_messages, 0, history_end
        )
        if summary_idx is not None:
            if summary_body and not self._previous_summary:
                self._previous_summary = summary_body
            turns = source_messages[max(compress_start, summary_idx + 1):history_end]
        else:
            turns = source_messages[compress_start:history_end]
        turn_prefix = (
            source_messages[split_turn_start:compress_end]
            if split_turn_start is not None else []
        )

        logger.info("Compressing turns %d-%d (%d turns), protecting %d head + %d tail",
                     compress_start + 1, compress_end, len(turns),
                     compress_start, n - compress_end)
        if self.hooks is not None:
            self.hooks.dispatch_pre_compact({
                "trigger": "manual" if force else "auto",
                "messages_before": n,
                "compress_start": compress_start,
                "compress_end": compress_end,
                "turns": len(turns),
            })

        # Phase 2: Generate summary
        if turns:
            summary = await self._generate_summary(
                turns,
                max_tokens=summary_budget,
                max_input_chars=summary_input_chars,
                replay=replay,
            )
        elif self._previous_summary:
            summary = f"{SUMMARY_PREFIX}\n{self._previous_summary}"
        else:
            summary = f"{SUMMARY_PREFIX}\nNo prior history."
        if summary and turn_prefix:
            prefix_summary = await self._generate_turn_prefix_summary(
                turn_prefix,
                max_tokens=max(64, summary_budget // 2),
                max_input_chars=max(256, summary_input_chars // 2),
            )
            if prefix_summary:
                body = summary[len(SUMMARY_PREFIX):].strip()
                combined = f"{body}\n\n---\n\n## Split Turn Context\n{prefix_summary}"
                self._previous_summary = combined
                summary = f"{SUMMARY_PREFIX}\n{combined}"
            else:
                summary = None
        if not summary:
            if not force or preserve_on_failure:
                # A failed summary must not silently delete history on the
                # opportunistic or admission-preview path. A normal forced
                # retry may still explicitly choose the static fallback.
                logger.info(
                    "Summary unavailable; preserving history "
                    "so history is not dropped without a summary"
                )
                return messages
            # Forced path: space must be freed even without a summary.
            # Drop middle with a static marker.
            n_dropped = compress_end - compress_start
            parts = [
                SUMMARY_PREFIX,
                f"Summary unavailable. {n_dropped} message(s) were removed "
                f"to free context space. Continue based on recent messages below.",
            ]
            # The LLM path protects explicit user preferences / active task
            # state; the static fallback must not silently drop them either.
            protected_items = self._extract_protected_content([*turns, *turn_prefix])
            if protected_items:
                items_text = "\n".join(f"- {item}" for item in protected_items)
                parts.append(f"Preserved user preferences & corrections (verbatim):\n{items_text}")
            active_task = self._extract_active_task_state([*turns, *turn_prefix])
            if active_task:
                parts.append(f"Active task state:\n{active_task}")
            summary = "\n\n".join(parts)

        # Phase 3: Assemble compressed message list
        compressed = [msg.copy() for msg in source_messages[:compress_start]]

        summary_role = self._choose_summary_role(
            str(source_messages[compress_start - 1].get("role") or "user") if compress_start > 0 else "user",
            str(source_messages[compress_end].get("role") or "user") if compress_end < n else "user",
        )
        compressed.append({"role": summary_role, "content": summary})

        # A split-turn summary is an aid for the work already performed in
        # the active turn. Keep the user's original request as an authoritative
        # message instead of asking the summary to stand in for it.
        if split_turn_start is not None:
            compressed.append(source_messages[split_turn_start].copy())

        for i in range(compress_end, n):
            compressed.append(source_messages[i].copy())

        # Phase 4: Truncate large tool call args + sanitize tool pairs
        compressed = self._truncate_tool_args(compressed)
        compressed = self._sanitize_tool_pairs(compressed)

        recap = self._build_compression_recap(
            summary,
            compressed[compress_start + 1:],
            max_prompt_tokens=max_prompt_tokens,
        )
        if recap:
            compressed.append(recap)

        self.compression_count += 1
        if self.hooks is not None:
            self.hooks.dispatch_post_compact({
                "trigger": "manual" if force else "auto",
                "messages_before": n,
                "messages_after": len(compressed),
                "compression_count": self.compression_count,
                "summary_chars": len(summary),
            })
        logger.info("Compression #%d: %d -> %d messages", self.compression_count, n, len(compressed))
        return compressed

    @staticmethod
    def _is_compression_recap(message: dict) -> bool:
        if str(message.get("provenance") or "").strip().lower() == _RECAP_PROVENANCE:
            return True
        content = message.get("content", "")
        return isinstance(content, str) and content.lstrip().startswith(
            COMPRESSION_RECAP_PREFIX
        )

    @staticmethod
    def _summary_section(summary: str, heading: str) -> str:
        match = re.search(
            rf"(?ms)^## {re.escape(heading)}\s*\n(.*?)(?=^## |\Z)",
            summary,
        )
        if not match:
            return ""
        body = match.group(1).strip()
        if not body or body.lower() in {"none", "none.", "n/a", "n/a."}:
            return ""
        return body[:_RECAP_SECTION_MAX_CHARS].rstrip()

    @classmethod
    def _build_compression_recap(
        cls,
        summary: str,
        preserved_tail: list[dict],
        *,
        max_prompt_tokens: int,
    ) -> dict | None:
        """Create one bounded tail observation after successful compaction.

        The latest real user request and its live tool chain are derived only
        from the preserved tail. Historical summary sections are explicitly
        labelled as earlier state so they cannot outrank the current request.
        """
        live_state = cls._extract_active_task_state(preserved_tail)
        sections: list[str] = []
        if live_state:
            sections.append(f"## Current Request and Live State\n{live_state}")
        for source_heading, recap_heading in _RECAP_SECTIONS:
            body = cls._summary_section(summary, source_heading)
            if body:
                sections.append(f"## {recap_heading}\n{body}")
        if not sections:
            return None
        body = "\n\n".join(sections)
        wrapper_overhead = len(
            COMPRESSION_RECAP_PREFIX
            + _RECAP_INTRO
            + COMPRESSION_RECAP_SUFFIX
        ) + 5
        total_budget = _RECAP_TOTAL_MAX_CHARS
        if max_prompt_tokens > 0:
            # The compressor already reserves up to 25% of tiny windows for
            # schema/estimation overhead. Keep recap inside that same reserve;
            # on very small windows, omit it rather than reintroduce overflow.
            total_budget = min(total_budget, max_prompt_tokens)
        body_budget = total_budget - wrapper_overhead
        if body_budget < _RECAP_MIN_BODY_CHARS:
            return None
        wrapper = (
            f"{COMPRESSION_RECAP_PREFIX}\n"
            f"{_RECAP_INTRO}\n\n"
            f"{body[:body_budget]}\n"
            f"{COMPRESSION_RECAP_SUFFIX}"
        )
        return {
            "role": "user",
            "content": wrapper,
            "provenance": _RECAP_PROVENANCE,
            "metadata": {"synthetic": True, "kind": "compression_recap"},
        }

    # ------------------------------------------------------------------
    # Boundary helpers
    # ------------------------------------------------------------------

    def _protect_head_size(self, messages: list[dict]) -> int:
        return 1 if messages and messages[0].get("role") == "system" else 0

    @staticmethod
    def _estimate_head_tokens(head: list[dict]) -> int:
        """Token estimate of the protected head (usually just the system prompt)."""
        return sum(estimate_value_tokens(msg) for msg in head)

    def _tail_budget_tokens(self, max_prompt_tokens: int, head_tokens: int) -> int:
        """Derive the tail budget from the real prompt budget, not a fixed constant.

        Keeps the configured ``tail_token_budget`` as the upper bound, but
        shrinks the tail when the model window cannot hold
        head + summary + tail + tool-schema overhead. Without this, compaction
        on small-window models still exceeds ``max_prompt_tokens`` after
        compression and the runner ends in a hard prompt-stop (react.py).
        """
        if max_prompt_tokens <= 0:
            return self.tail_token_budget
        # Reserve model-window space for the generated summary plus tool schema
        # and estimation error.  The reserve itself must fit tiny windows.
        safety = min(max_prompt_tokens // 4, max(_MIN_SAFETY_BUDGET, max_prompt_tokens // 16))
        available = (
            max_prompt_tokens
            - head_tokens
            - self._summary_output_budget(max_prompt_tokens)
            - safety
        )
        # A zero history-tail budget is valid: _find_tail_cut still preserves
        # the latest user turn and its active tool chain.  Enforcing a 2K floor
        # here made the allocation mathematically exceed 1K prompt windows.
        return int(max(0, min(self.tail_token_budget, available)))

    def _summary_output_budget(self, max_prompt_tokens: int) -> int:
        """Bound summary output to a fraction of the active prompt budget."""
        if max_prompt_tokens <= 0:
            return self.summary_max_tokens
        proportional = max(1, int(max_prompt_tokens * _SUMMARY_TARGET_RATIO))
        return min(self.summary_max_tokens, proportional)

    @staticmethod
    def _summary_input_chars_budget(max_prompt_tokens: int) -> int:
        """Leave roughly half the prompt budget for template/protection overhead."""
        if max_prompt_tokens <= 0:
            return _SUMMARY_INPUT_CHARS_CEILING
        proportional = max_prompt_tokens * _CHARS_PER_TOKEN // 2
        return min(_SUMMARY_INPUT_CHARS_CEILING, proportional)

    def _find_tail_cut(self, messages: list[dict], start: int,
                       tail_budget_tokens: int | None = None) -> int:
        """Find tail start without ever compacting the active user turn."""
        cut, split_turn_start = self._find_tail_plan(messages, start, tail_budget_tokens)
        return split_turn_start if split_turn_start is not None else cut

    def _find_tail_plan(
        self,
        messages: list[dict],
        start: int,
        tail_budget_tokens: int | None = None,
    ) -> tuple[int, int | None]:
        """Return the retained-tail boundary and an optional split-turn start.

        A huge active turn may be split only at a complete message/tool group.
        Its user request and early work receive a second dedicated summary.
        """
        budget_tokens = (tail_budget_tokens if tail_budget_tokens is not None
                         else self.tail_token_budget)
        budget = budget_tokens * _CHARS_PER_TOKEN
        accumulated = 0
        latest_user = next(
            (i for i in range(len(messages) - 1, start - 1, -1)
             if messages[i].get("role") == "user"
             and not _is_synthetic_user_turn(messages[i])),
            start,
        )
        for i in range(len(messages) - 1, start - 1, -1):
            accumulated += self._msg_char_len(messages[i])
            if accumulated >= budget and i > start:
                aligned = self._align_tail_boundary(messages, i)
                if aligned > latest_user:
                    return aligned, latest_user
                return min(aligned, latest_user), None
        # Even when the configured tail budget can hold everything, a forced
        # or message-count compaction may still be needed. Compact only the
        # history before the latest user request.
        return latest_user, None

    def _align_tail_boundary(self, messages: list[dict], idx: int) -> int:
        """Walk backward to avoid splitting a tool_call/result group."""
        if idx <= 0 or idx >= len(messages):
            return idx
        check = idx - 1
        while check >= 0 and messages[check].get("role") == "tool":
            check -= 1
        if check >= 0 and messages[check].get("role") == "assistant" and messages[check].get("tool_calls"):
            return check
        return idx

    @staticmethod
    def _msg_char_len(msg: dict) -> int:
        content = msg.get("content", "") or ""
        if isinstance(content, str):
            return len(content)
        if isinstance(content, list):
            return sum(len(p.get("text", "")) if isinstance(p, dict) else len(str(p)) for p in content)
        return len(str(content))

    # ------------------------------------------------------------------
    # Protected content extraction & validation
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_protected_content(turns: list[dict]) -> list[str]:
        """Deterministically extract user preferences, corrections, and directives.

        Only user messages that match _PREFERENCE_RE are captured — no LLM
        inference, matching the project's conservative auto-retain policy.
        Returns a deduplicated list of short verbatim snippets.
        """
        items: list[str] = []
        seen: set[str] = set()
        for msg in turns:
            if msg.get("role") != "user" or _is_synthetic_user_turn(msg):
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p) for p in content
                )
            content = str(content).strip()
            if len(content) < _PROTECTED_MIN_MSG_LEN:
                continue
            if not _PREFERENCE_RE.search(content):
                continue
            # Truncate very long messages to the protected budget
            snippet = content[:_PROTECTED_ITEM_MAX].strip()
            key = snippet[:80].lower()
            if key in seen:
                continue
            seen.add(key)
            items.append(snippet)
            if len(items) >= _PROTECTED_MAX_ITEMS:
                break
        return items

    @staticmethod
    def _extract_active_task_state(turns: list[dict]) -> str:
        """Extract the latest user request and its tool chain status.

        Walks backward from the end of turns to find the last user message,
        then collects the assistant/tool responses that follow it to capture
        the current execution state.
        """
        last_user_idx = None
        for i in range(len(turns) - 1, -1, -1):
            if turns[i].get("role") == "user" and not _is_synthetic_user_turn(turns[i]):
                content = turns[i].get("content", "")
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") if isinstance(p, dict) else str(p) for p in content
                    )
                content = str(content).strip()
                if len(content) >= _PROTECTED_MIN_MSG_LEN:
                    last_user_idx = i
                    break
        if last_user_idx is None:
            return ""

        parts: list[str] = []
        user_content = turns[last_user_idx].get("content", "")
        if isinstance(user_content, list):
            user_content = " ".join(
                p.get("text", "") if isinstance(p, dict) else str(p) for p in user_content
            )
        parts.append(f"User request: {str(user_content).strip()[:300]}")

        # Collect tool results after the user request
        for msg in turns[last_user_idx + 1:]:
            role = msg.get("role", "")
            if role == "tool":
                tool_name = msg.get("name", msg.get("tool_name", "tool"))
                tool_content = str(msg.get("content", ""))[:200]
                parts.append(f"  [{tool_name}] {tool_content}")
            elif role == "assistant" and msg.get("tool_calls"):
                tc_names = ", ".join(
                    tc.get("function", {}).get("name", "?") for tc in msg["tool_calls"]
                )
                parts.append(f"  [called] {tc_names}")

        return "\n".join(parts)

    @staticmethod
    def _validate_protected_survival(summary: str, protected_items: list[str]) -> list[str]:
        """Check which protected items survived in the summary via 3-gram overlap.

        Returns the list of items that did NOT survive and need appending.
        """
        if not protected_items:
            return []

        def trigrams(text: str) -> set[str]:
            normalized = re.sub(r"\s+", " ", text.lower().strip())
            return {normalized[i:i+3] for i in range(len(normalized) - 2)} if len(normalized) >= 3 else {normalized}

        summary_grams = trigrams(summary)
        missing: list[str] = []
        for item in protected_items:
            item_grams = trigrams(item)
            if not item_grams:
                continue
            overlap = len(item_grams & summary_grams) / len(item_grams)
            # Require at least 60% 3-gram overlap to consider it survived
            if overlap < 0.6:
                missing.append(item)
        return missing

    # ------------------------------------------------------------------
    # Summary generation
    # ------------------------------------------------------------------

    async def _generate_summary(
        self,
        turns: list[dict],
        *,
        max_tokens: int | None = None,
        max_input_chars: int | None = None,
        replay: dict | None = None,
    ) -> str | None:
        # Check cooldown
        if time.monotonic() < self._failure_cooldown_until:
            return None

        content = self._format_turns_for_summary(turns, max_chars=max_input_chars)

        # Phase 0: Extract protected content BEFORE summarization
        protected_items = self._extract_protected_content(turns)
        active_task = self._extract_active_task_state(turns)

        protection_block = ""
        if protected_items:
            items_text = "\n".join(f"  - {item}" for item in protected_items)
            protection_block += (
                f"\n\n## MUST PRESERVE VERBATIM — User Preferences & Corrections\n"
                f"The following user statements are EXPLICIT preferences, corrections, "
                f"or directives. They MUST appear VERBATIM (word-for-word, same language) "
                f"in the summary under '## Critical Context'. Do NOT paraphrase, soften, "
                f"or convert them to third-person speculation:\n{items_text}\n"
            )
        if active_task:
            protection_block += (
                f"\n## MUST PRESERVE — Active Task State\n"
                f"The following is the latest user request and its execution state. "
                f"It MUST be reflected accurately in '## Active Task' with specific "
                f"details preserved:\n{active_task}\n"
            )

        if self._previous_summary:
            prompt = (
                f"You are updating a context compaction summary. A previous compaction "
                f"produced the summary below. New conversation turns have occurred since "
                f"then and need to be incorporated.\n\n"
                f"PREVIOUS SUMMARY:\n{self._previous_summary}\n\n"
                f"NEW TURNS TO INCORPORATE:\n{content}\n"
                f"{protection_block}\n"
                f"Update the summary using this exact structure. PRESERVE all existing "
                f"information that is still relevant. ADD new completed actions. "
                f"Move answered questions to Resolved Questions. "
                f"CRITICAL: Update '## Active Task' to reflect the most recent "
                f"unfulfilled request.\n\n{_TEMPLATE}"
            )
        else:
            prompt = (
                f"Create a structured checkpoint summary for the conversation below. "
                f"Preserve enough detail for continuity.\n\n"
                f"TURNS TO SUMMARIZE:\n{content}\n"
                f"{protection_block}\n"
                f"Use this exact structure:\n\n{_TEMPLATE}"
            )

        output_tokens = self.summary_max_tokens if max_tokens is None else max_tokens
        summary = (
            await self._summary_from_replay(replay, protection_block, output_tokens)
            if replay is not None else None
        )
        try:
            if summary is None:
                response = await self.llm.chat_limited(
                    [{"role": "user", "content": prompt}],
                    max_tokens=output_tokens,
                    temperature=0.1,
                    disable_thinking=True,
                )
                summary = response.get("content", "").strip()
            if not summary:
                return None

            # Phase 2.5: Post-validation — append any protected items that
            # the LLM paraphrased away or dropped entirely.
            missing = self._validate_protected_survival(summary, protected_items)
            if missing:
                logger.info("Protected content validation: %d/%d items missing, appending",
                            len(missing), len(protected_items))
                missing_block = "\n".join(f"- {item}" for item in missing)
                summary += (
                    f"\n\n## Protected Context (verbatim — do not paraphrase)\n"
                    f"{missing_block}"
                )

            self._previous_summary = summary
            self._failure_cooldown_until = 0.0
            return f"{SUMMARY_PREFIX}\n{summary}"
        except Exception as e:
            logger.warning("Summary generation failed: %s", e)
            self._failure_cooldown_until = time.monotonic() + _SUMMARY_FAILURE_COOLDOWN
            return None

    async def _summary_from_replay(self, replay: dict, protection_block: str, max_tokens: int) -> str | None:
        """Ask for the summary right after the conversation itself, as last sent.

        The request repeats the last request byte for byte (system, tools, history),
        adds what came after it, and ends with the checkpoint instruction, so the
        provider bills the repeated part at its cached rate and the model reads the
        original messages rather than a transcript. None when the request would not
        fit the prompt budget, the call fails, or the model answers with a tool call;
        the caller then summarizes from a transcript as before.
        """
        carry = (
            "The conversation contains an earlier checkpoint summary: carry forward everything in "
            "it that is still relevant, add what happened since, and move answered questions to "
            "Resolved Questions. "
            if self._previous_summary else ""
        )
        instruction = (
            "[Context checkpoint request from the runtime, not from the user]\n"
            "The older part of the conversation above is about to be replaced by a checkpoint "
            "summary that you write now; the most recent messages stay as they are. Do not call "
            "any tool and do not continue the task: reply with the summary only. "
            f"{carry}Preserve enough detail for continuity. CRITICAL: '## Active Task' must "
            f"reflect the most recent unfulfilled request.{protection_block}\n\n"
            f"Use this exact structure:\n\n{_TEMPLATE}"
        )
        messages = [*replay.get("messages", []), {"role": "user", "content": instruction}]
        tools = list(replay.get("tools") or [])
        limit = int(replay.get("limit") or 0)
        estimate = getattr(self.llm, "estimate_tokens", None)
        if estimate is None or limit <= 0:
            return None
        try:
            needed = int(estimate(messages)) + estimate_value_tokens(tools)
        except Exception:
            return None
        if needed > limit:
            logger.info("Summary request would not fit replayed (%d > %d tokens); using a transcript", needed, limit)
            return None
        try:
            # Thinking stays as the conversation has it: switching it off renders the whole
            # prompt differently for DeepSeek, and nothing of the prefix is then cached.
            response = await self.llm.chat_limited(
                messages,
                tools or None,
                max_tokens=max_tokens,
                temperature=0.1,
            )
        except Exception as exc:
            logger.info("Replayed summary request failed (%s); using a transcript", exc)
            return None
        if response.get("tool_calls"):
            logger.info("Replayed summary request answered with a tool call; using a transcript")
            return None
        usage = response.get("usage") or {}
        logger.info(
            "Summary replayed the last request: %s prompt tokens, %s from cache",
            usage.get("prompt_tokens", "?"), usage.get("prompt_cache_hit_tokens", "?"),
        )
        return str(response.get("content") or "").strip() or None

    async def _generate_turn_prefix_summary(
        self,
        turns: list[dict],
        *,
        max_tokens: int,
        max_input_chars: int,
    ) -> str | None:
        """Summarize only the discarded prefix of an oversized active turn."""
        content = self._format_turns_for_summary(turns, max_chars=max_input_chars)
        prompt = (
            "The following messages are the PREFIX of one active turn whose recent suffix "
            "will remain verbatim. Summarize only what the suffix needs to continue. Do not "
            "answer the request or invent tool outcomes. Use exactly these headings:\n\n"
            "## Original Request\n## Early Progress\n## Context for Retained Suffix\n\n"
            f"TURN PREFIX:\n{content}"
        )
        try:
            response = await self.llm.chat_limited(
                [{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=0.1,
                disable_thinking=True,
            )
            return str(response.get("content") or "").strip() or None
        except Exception as exc:
            logger.warning("Split-turn prefix summarization failed: %s", exc)
            self._failure_cooldown_until = time.monotonic() + _SUMMARY_FAILURE_COOLDOWN
            return None

    def _format_turns_for_summary(
        self,
        turns: list[dict],
        *,
        max_chars: int | None = None,
    ) -> str:
        lines = []
        for msg in turns:
            role = msg.get("role", "?")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") if isinstance(p, dict) else str(p) for p in content
                )
            content = str(content).strip() if content else ""

            if role == "tool":
                tool_name = msg.get("name", msg.get("tool_name", "tool"))
                tool_id = msg.get("tool_call_id", "")
                lines.append(f"[tool] {tool_name} (id={tool_id}): {content[:300]}")
            elif role == "assistant":
                tool_calls = msg.get("tool_calls")
                if tool_calls:
                    tc_info = ", ".join(
                        f"{tc.get('function', {}).get('name', '?')}"
                        for tc in tool_calls
                    )
                    lines.append(f"[assistant] called: {tc_info}")
                if content:
                    lines.append(f"[assistant] {content[:500]}")
            else:
                lines.append(f"[{role}] {content[:500]}")
        char_budget = _SUMMARY_INPUT_CHARS_CEILING if max_chars is None else max(0, max_chars)
        text = "\n".join(lines)
        if len(text) <= char_budget:
            return text
        # Cap the summary prompt itself: an unbounded turn list can overflow
        # the model window before summarization even starts (the LLM then
        # errors, enters cooldown, and history is dropped without a summary).
        # Keep the newest lines — they are most relevant to the active task,
        # and iterative compaction already carries older history in the
        # previous summary.
        kept: list[str] = []
        used = 0
        for line in reversed(lines):
            cost = len(line) + 1
            if used + cost > char_budget:
                break
            kept.append(line)
            used += cost
        kept.reverse()
        omitted = len(lines) - len(kept)
        return (
            f"[earlier {omitted} message line(s) omitted from summary input "
            f"to stay within the model window]\n"
            + "\n".join(kept)
        )

    # ------------------------------------------------------------------
    # Iterative summary detection
    # ------------------------------------------------------------------

    def _find_existing_summary(self, messages: list[dict], start: int, end: int) -> tuple:
        """Find the newest compaction summary in a range of messages."""
        for i in range(end - 1, start - 1, -1):
            content = messages[i].get("content", "")
            text = content if isinstance(content, str) else str(content)
            if text.startswith(SUMMARY_PREFIX):
                return i, text[len(SUMMARY_PREFIX):].strip()
        return None, ""

    # ------------------------------------------------------------------
    # Role selection
    # ------------------------------------------------------------------

    @staticmethod
    def _choose_summary_role(head_role: str, tail_role: str) -> str:
        """Pick a role that avoids consecutive same-role with both neighbors."""
        if head_role in {"assistant", "tool"}:
            role = "user"
        else:
            role = "assistant"
        if role == tail_role:
            flipped = "assistant" if role == "user" else "user"
            if flipped != head_role:
                return flipped
        return role

    # ------------------------------------------------------------------
    # Tool pair sanitization
    # ------------------------------------------------------------------

    @staticmethod
    def _truncate_tool_args(messages: list[dict], max_arg_len: int = 500) -> list[dict]:
        """Truncate large tool call arguments to prevent JSON parse errors.

        If arguments are valid JSON, recursively shrink long string values
        while preserving the JSON structure.  Otherwise fall back to raw
        truncation with a placeholder.
        """
        for msg in messages:
            for tc in msg.get("tool_calls") or []:
                args = tc.get("function", {}).get("arguments", "")
                if not isinstance(args, str) or len(args) <= max_arg_len:
                    continue
                try:
                    parsed = json.loads(args)
                except (json.JSONDecodeError, TypeError):
                    tc["function"]["arguments"] = args[:max_arg_len] + "...[truncated]"
                    continue

                def _shrink(obj):
                    if isinstance(obj, str) and len(obj) > max_arg_len:
                        return obj[:max_arg_len] + "...[truncated]"
                    if isinstance(obj, dict):
                        return {k: _shrink(v) for k, v in obj.items()}
                    if isinstance(obj, list):
                        return [_shrink(v) for v in obj]
                    return obj

                tc["function"]["arguments"] = json.dumps(
                    _shrink(parsed), ensure_ascii=False,
                )
        return messages

    def _sanitize_tool_pairs(self, messages: list[dict]) -> list[dict]:
        """Fix orphaned tool_call/tool_result pairs after compression."""
        # Collect surviving call IDs
        call_ids: set = set()
        for msg in messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    cid = tc.get("id")
                    if cid:
                        call_ids.add(cid)

        # Remove orphaned tool results
        result_ids: set = set()
        for msg in messages:
            if msg.get("role") == "tool":
                cid = msg.get("tool_call_id")
                if cid:
                    result_ids.add(cid)
        orphaned = result_ids - call_ids
        if orphaned:
            messages = [m for m in messages
                        if not (m.get("role") == "tool" and m.get("tool_call_id") in orphaned)]

        # Add stub results for orphaned tool calls
        missing = call_ids - {m.get("tool_call_id") for m in messages if m.get("role") == "tool"}
        if missing:
            result: list[dict] = []
            for msg in messages:
                result.append(msg)
                if msg.get("role") == "assistant":
                    for tc in msg.get("tool_calls") or []:
                        cid = tc.get("id")
                        if cid in missing:
                            result.append({
                                "role": "tool",
                                "content": "[Result from earlier conversation — see context summary above]",
                                "tool_call_id": cid,
                            })
            messages = result

        return messages
