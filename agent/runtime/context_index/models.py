"""Value objects shared by context-index producers and consumers."""

from dataclasses import dataclass, field
from typing import Literal

ContextSource = Literal["session", "activity", "habit", "memory"]
TrustLabel = Literal["historical_context", "untrusted_observation", "inferred_pattern"]


@dataclass(frozen=True)
class SourceLocator:
    kind: Literal["session_message", "activity_summary", "activity_event", "habit", "memory_record"]
    primary: str = field(repr=False)
    secondary: int = field(default=0, repr=False)
    revision: str = field(default="", repr=False)


@dataclass(frozen=True)
class RecommendationCandidate:
    source: ContextSource
    identity: str = field(repr=False)
    description: str
    timestamp: float
    workspace_tier: int
    native_query_rank: float
    topic_key: str
    trust_label: TrustLabel
    locator: SourceLocator = field(repr=False)
    private_text: str = field(default="", repr=False)
    project_label: str = ""
    cache_state: str = ""
    support_days: int = 0
    support_weeks: int = 0
    slot: str = ""
    reason: str = ""
    channel_ranks: tuple[tuple[str, int], ...] = ()
    revision: str = field(default="", repr=False)
    available_at: float = 0.0
    utility: float = 0.0


@dataclass(frozen=True)
class RetrievalStage:
    name: str
    latency_ms: float
    error_category: str = ""


@dataclass(frozen=True)
class SourceResult:
    availability: str
    relevance: tuple[RecommendationCandidate, ...] = ()
    recency: tuple[RecommendationCandidate, ...] = ()
    habit: RecommendationCandidate | None = None
    cache_state: str = ""
    error_category: str = ""
    diagnostics: tuple[str, ...] = ()
    stages: tuple[RetrievalStage, ...] = ()

    @property
    def all_candidates(self) -> tuple[RecommendationCandidate, ...]:
        habit = (self.habit,) if self.habit is not None else ()
        return self.relevance + self.recency + habit


@dataclass(frozen=True)
class EvidenceResult:
    source: ContextSource
    trust_label: TrustLabel
    title: str
    items: tuple[str, ...]


@dataclass(frozen=True)
class ContextIndexRow:
    handle: str
    source: ContextSource
    slot: str
    description: str
    reason: str
    project_label: str = ""
    cache_state: str = ""
    timestamp: float = 0.0
    channels: tuple[str, ...] = ()
    # The record id of a structured-memory row, for usage counts.
    memory_id: str = ""


@dataclass(frozen=True)
class ContextIndexPack:
    request_id: str
    rendered: str
    rows: tuple[ContextIndexRow, ...] = ()


@dataclass(frozen=True)
class HandleEntry:
    request_id: str
    source: ContextSource
    locator: SourceLocator = field(repr=False)


@dataclass
class ContextIndexTrace:
    request_id: str
    session_id: str
    mode: str
    workspace_label: str
    displayed: list[ContextIndexRow] = field(default_factory=list)
    opened: list[tuple[str, str]] = field(default_factory=list)
    source_status: dict[str, str] = field(default_factory=dict)
    source_errors: dict[str, str] = field(default_factory=dict)
    source_diagnostics: dict[str, tuple[str, ...]] = field(default_factory=dict)
    latency_ms: float = 0.0
    rendered_chars: int = 0
    intent: str = "lookup"
    decision: str = "no-match"
    candidate_count: int = 0
    rendered_tokens: int = 0
    opened_tokens: int = 0
    submitted: bool = False
    semantic_status: dict[str, str] = field(default_factory=dict)
    semantic_errors: dict[str, str] = field(default_factory=dict)
    source_stages: dict[str, tuple[RetrievalStage, ...]] = field(default_factory=dict)
    semantic_stages: dict[str, tuple[RetrievalStage, ...]] = field(default_factory=dict)
    selected_count: int = 0
    max_items: int = 4
    char_budget: int = 900
    render_omissions: dict[str, tuple[str, ...]] = field(default_factory=dict)
