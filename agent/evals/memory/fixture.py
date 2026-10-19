"""Scenario schema, validation and evidence fragments for the memory evaluation.

A scenario is a seeded world (core Markdown entries, structured memory records
and a session archive) plus one multi-turn conversation whose turns are probes.
Evidence is referenced as ``rec:<record>``, ``msg:<session>.<message>`` or
``text:<snippet>``. A record or archive message counts as injected when its
*fragment* (by default the first characters of its normalized content, which
every injection path shows first) appears in the injected text.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FIXTURE = Path(__file__).with_name("data") / "scenarios-v1.json"
CATEGORIES = (
    "single_fact",
    "preference_application",
    "knowledge_update",
    "temporal",
    "multi_session",
    "abstention",
    "cross_lingual",
    "chinese_short_terms",
    "unrelated",
)
# Probes in these categories must not receive scenario memory beyond core.
NO_EVIDENCE_CATEGORIES = frozenset({"abstention", "unrelated"})
RECORD_KINDS = ("persona", "user_fact", "preference", "episode", "observation", "task_ref")
FRAGMENT_CHARS = 16
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def normalize(text: str) -> str:
    """Case, width, entity and whitespace-insensitive form used for matching."""
    value = html.unescape(unicodedata.normalize("NFKC", str(text)))
    return " ".join(value.casefold().split())


@dataclass(frozen=True)
class LiveSpec:
    must: tuple[tuple[str, ...], ...] = ()
    stale: tuple[str, ...] = ()
    forbid: tuple[str, ...] = ()
    abstain: bool = False

    @property
    def scored(self) -> bool:
        return bool(self.must or self.forbid or self.abstain)


@dataclass(frozen=True)
class RecordSpec:
    key: str
    kind: str
    content: str
    days_ago: int
    at: str
    supersedes: str = ""
    source: str = ""
    salience: float = 0.6
    tags: tuple[str, ...] = ()
    metadata: tuple[tuple[str, Any], ...] = ()
    fragment: str = ""

    @property
    def ref(self) -> str:
        return f"rec:{self.key}"


@dataclass(frozen=True)
class MessageSpec:
    key: str
    role: str
    content: str
    fragment: str = ""


@dataclass(frozen=True)
class SessionSpec:
    key: str
    days_ago: int
    at: str
    workspace: str
    messages: tuple[MessageSpec, ...]


@dataclass(frozen=True)
class TurnSpec:
    text: str
    id: str = ""
    category: str = ""
    expect: tuple[tuple[str, ...], ...] = ()
    stale: tuple[str, ...] = ()
    allowed: tuple[str, ...] = ()
    live: LiveSpec = LiveSpec()

    @property
    def is_probe(self) -> bool:
        return bool(self.id)


@dataclass(frozen=True)
class ConsolidationExpect:
    """A change one consolidation pass should propose: text matching every group, optionally as a revision."""

    must: tuple[tuple[str, ...], ...]
    revises: str = ""


@dataclass(frozen=True)
class ConsolidationSpec:
    # Records left out of the seeded world, so the archive is ahead of memory.
    without: tuple[str, ...] = ()
    expect: tuple[ConsolidationExpect, ...] = ()


@dataclass(frozen=True)
class ScenarioSpec:
    id: str
    title: str
    core_user: tuple[str, ...]
    core_memory: tuple[str, ...]
    records: tuple[RecordSpec, ...]
    archive: tuple[SessionSpec, ...]
    conversation: tuple[TurnSpec, ...]
    consolidation: ConsolidationSpec | None = None

    @property
    def probes(self) -> tuple[TurnSpec, ...]:
        return tuple(turn for turn in self.conversation if turn.is_probe)

    def item_contents(self) -> dict[str, tuple[str, str]]:
        """Map each seeded record/message ref to (content, fragment)."""
        items: dict[str, tuple[str, str]] = {}
        for record in self.records:
            items[record.ref] = (record.content, record.fragment or default_fragment(record.content))
        for session in self.archive:
            for message in session.messages:
                ref = f"msg:{session.key}.{message.key}"
                items[ref] = (message.content, message.fragment or default_fragment(message.content))
        return items


@dataclass(frozen=True)
class WritePathSpec:
    id: str
    sessions: tuple[tuple[str, ...], ...]
    ask: str
    live: LiveSpec


@dataclass(frozen=True)
class Fixture:
    schema_version: int
    provenance: str
    scenarios: tuple[ScenarioSpec, ...]
    write_path: tuple[WritePathSpec, ...]
    sha256: str

    @property
    def probes(self) -> tuple[TurnSpec, ...]:
        return tuple(probe for scenario in self.scenarios for probe in scenario.probes)

    def select(self, scenario_ids: list[str] | None) -> Fixture:
        if not scenario_ids:
            return self
        known = {scenario.id for scenario in self.scenarios}
        unknown = sorted(set(scenario_ids) - known)
        if unknown:
            raise ValueError(f"Unknown scenario id(s): {', '.join(unknown)}")
        chosen = tuple(scenario for scenario in self.scenarios if scenario.id in set(scenario_ids))
        return Fixture(self.schema_version, self.provenance, chosen, self.write_path, self.sha256)


def default_fragment(content: str) -> str:
    return normalize(content)[:FRAGMENT_CHARS].strip()


def _strings(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f"{label} must be a list of non-empty strings")
    return tuple(value)


def _live(raw: Any, label: str) -> LiveSpec:
    if raw is None:
        return LiveSpec()
    if not isinstance(raw, dict):
        raise ValueError(f"{label}.live must be an object")
    must = raw.get("must") or []
    if not isinstance(must, list):
        raise ValueError(f"{label}.live.must must be a list of alternative lists")
    groups = tuple(_strings(group, f"{label}.live.must[]") for group in must)
    if any(not group for group in groups):
        raise ValueError(f"{label}.live.must contains an empty group")
    return LiveSpec(
        must=groups,
        stale=_strings(raw.get("stale"), f"{label}.live.stale"),
        forbid=_strings(raw.get("forbid"), f"{label}.live.forbid"),
        abstain=bool(raw.get("abstain", False)),
    )


def _expect(raw: Any, label: str) -> tuple[tuple[str, ...], ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"{label}.expect must be a list")
    groups: list[tuple[str, ...]] = []
    for item in raw:
        group = (item,) if isinstance(item, str) else _strings(item, f"{label}.expect[]")
        if not group:
            raise ValueError(f"{label}.expect contains an empty alternative list")
        groups.append(group)
    return tuple(groups)


def _when(raw: dict, label: str) -> tuple[int, str]:
    days = raw.get("days_ago")
    at = raw.get("at")
    if not isinstance(days, int) or days < 1 or days > 400:
        raise ValueError(f"{label}.days_ago must be an integer from 1 to 400")
    if not isinstance(at, str) or not _TIME.match(at):
        raise ValueError(f"{label}.at must be HH:MM")
    return days, at


def _parse_scenario(raw: dict) -> ScenarioSpec:
    scenario_id = str(raw.get("id") or "")
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,40}", scenario_id):
        raise ValueError(f"Invalid scenario id: {scenario_id!r}")
    core = raw.get("core") or {}
    records: list[RecordSpec] = []
    kinds: dict[str, str] = {}
    for item in raw.get("records") or []:
        label = f"{scenario_id}.records.{item.get('key')}"
        days, at = _when(item, label)
        supersedes = str(item.get("supersedes") or "")
        kind = str(item.get("kind") or (kinds.get(supersedes, "") if supersedes else ""))
        if kind not in RECORD_KINDS:
            raise ValueError(f"{label}: unknown or missing kind {kind!r}")
        key = str(item["key"])
        kinds[key] = kind
        metadata = item.get("metadata") or {}
        if not isinstance(metadata, dict):
            raise ValueError(f"{label}.metadata must be an object")
        records.append(RecordSpec(
            key=key, kind=kind, content=str(item["content"]), days_ago=days, at=at,
            supersedes=supersedes, source=str(item.get("source") or ""),
            salience=float(item.get("salience", 0.6)), tags=_strings(item.get("tags"), f"{label}.tags"),
            metadata=tuple(sorted(metadata.items())), fragment=normalize(item.get("fragment") or ""),
        ))
    archive: list[SessionSpec] = []
    for session in raw.get("archive") or []:
        label = f"{scenario_id}.archive.{session.get('key')}"
        days, at = _when(session, label)
        workspace = str(session.get("workspace") or "current")
        if workspace not in {"current", "other"}:
            raise ValueError(f"{label}.workspace must be current or other")
        messages = []
        for message in session.get("messages") or []:
            role = str(message.get("role"))
            if role not in {"user", "assistant"}:
                raise ValueError(f"{label}: message role must be user or assistant")
            messages.append(MessageSpec(
                str(message["key"]), role, str(message["content"]), normalize(message.get("fragment") or ""),
            ))
        if not messages:
            raise ValueError(f"{label} has no messages")
        archive.append(SessionSpec(str(session["key"]), days, at, workspace, tuple(messages)))
    conversation: list[TurnSpec] = []
    for index, turn in enumerate(raw.get("conversation") or []):
        text = str(turn.get("text") or "").strip()
        if not text:
            raise ValueError(f"{scenario_id}.conversation[{index}] has no text")
        if turn.get("filler"):
            conversation.append(TurnSpec(text=text))
            continue
        label = f"{scenario_id}.conversation.{turn.get('id')}"
        category = str(turn.get("category") or "")
        if category not in CATEGORIES:
            raise ValueError(f"{label}: unknown category {category!r}")
        conversation.append(TurnSpec(
            text=text, id=str(turn["id"]), category=category, expect=_expect(turn.get("expect"), label),
            stale=_strings(turn.get("stale"), f"{label}.stale"),
            allowed=_strings(turn.get("allowed"), f"{label}.allowed"), live=_live(turn.get("live"), label),
        ))
    return ScenarioSpec(
        id=scenario_id, title=str(raw.get("title") or scenario_id),
        core_user=_strings(core.get("user"), f"{scenario_id}.core.user"),
        core_memory=_strings(core.get("memory"), f"{scenario_id}.core.memory"),
        records=tuple(records), archive=tuple(archive), conversation=tuple(conversation),
        consolidation=_consolidation(raw.get("consolidation"), f"{scenario_id}.consolidation"),
    )


def _consolidation(raw: object, label: str) -> ConsolidationSpec | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{label} must be an object")
    expects = []
    for index, item in enumerate(raw.get("expect") or []):
        must = tuple(_strings(group, f"{label}.expect[{index}].must[]") for group in item.get("must") or [])
        if not must or not all(must):
            raise ValueError(f"{label}.expect[{index}] needs non-empty must groups")
        expects.append(ConsolidationExpect(must, str(item.get("revises") or "")))
    return ConsolidationSpec(_strings(raw.get("without"), f"{label}.without"), tuple(expects))


def validate_scenario(scenario: ScenarioSpec) -> None:
    """Reject ambiguous evidence so a fragment match identifies one seeded item."""
    items = scenario.item_contents()
    record_keys = [record.key for record in scenario.records]
    if len(record_keys) != len(set(record_keys)):
        raise ValueError(f"{scenario.id}: duplicate record keys")
    session_keys = [session.key for session in scenario.archive]
    if len(session_keys) != len(set(session_keys)):
        raise ValueError(f"{scenario.id}: duplicate archive session keys")
    by_key = {record.key: record for record in scenario.records}
    if scenario.consolidation is not None:
        named = [*scenario.consolidation.without,
                 *(item.revises for item in scenario.consolidation.expect if item.revises)]
        if unknown := [key for key in named if key not in by_key]:
            raise ValueError(f"{scenario.id}.consolidation names unknown records {unknown}")
    for record in scenario.records:
        if record.supersedes:
            old = by_key.get(record.supersedes)
            if old is None or old.days_ago <= record.days_ago:
                raise ValueError(f"{scenario.id}.{record.key}: must supersede an older record")
        if record.source and f"msg:{record.source}" not in items:
            raise ValueError(f"{scenario.id}.{record.key}: unknown source {record.source}")
    core_text = normalize("\n".join(scenario.core_user + scenario.core_memory))
    turn_texts = [normalize(turn.text) for turn in scenario.conversation]
    fragments: dict[str, str] = {}
    for ref, (_content, fragment) in items.items():
        if len(fragment) < 6:
            raise ValueError(f"{scenario.id}.{ref}: fragment is too short to be distinctive")
        if fragment in fragments:
            raise ValueError(f"{scenario.id}: {ref} and {fragments[fragment]} share fragment {fragment!r}")
        fragments[fragment] = ref
        if fragment in core_text:
            raise ValueError(f"{scenario.id}.{ref}: fragment also appears in core memory")
        if any(fragment in text for text in turn_texts):
            raise ValueError(f"{scenario.id}.{ref}: fragment appears in a conversation turn")
    for ref, (_content, fragment) in items.items():
        for other, (other_content, _other_fragment) in items.items():
            if other != ref and fragment in normalize(other_content):
                raise ValueError(f"{scenario.id}: fragment of {ref} also occurs inside {other}")
    seeded = [core_text, *(normalize(content) for content, _fragment in items.values())]
    for turn in scenario.probes:
        refs = [ref for group in turn.expect for ref in group] + list(turn.allowed)
        for ref in refs:
            if ref.startswith("text:"):
                snippet = normalize(ref[5:])
                if not snippet or not any(snippet in text for text in seeded):
                    raise ValueError(f"{turn.id}: evidence snippet {ref!r} is not seeded")
            elif ref not in items:
                raise ValueError(f"{turn.id}: unknown evidence ref {ref!r}")
        for snippet in turn.stale:
            if not any(normalize(snippet) in text for text in seeded):
                raise ValueError(f"{turn.id}: stale snippet {snippet!r} is not seeded")
        if turn.category in NO_EVIDENCE_CATEGORIES and turn.expect:
            raise ValueError(f"{turn.id}: {turn.category} probes must not expect evidence")
        if turn.category not in NO_EVIDENCE_CATEGORIES and not turn.expect:
            raise ValueError(f"{turn.id}: {turn.category} probes need expected evidence")
        for group in turn.live.must:
            for keyword in group:
                if keyword.startswith("date:") and keyword[5:] not in session_keys:
                    raise ValueError(f"{turn.id}: live date keyword names unknown session {keyword!r}")


def load_fixture(path: Path = FIXTURE) -> Fixture:
    data = path.read_bytes()
    raw = json.loads(data.decode("utf-8"))
    if raw.get("schema_version") != 1 or not raw.get("scenarios"):
        raise ValueError("Unsupported memory evaluation fixture")
    scenarios = tuple(_parse_scenario(item) for item in raw["scenarios"])
    ids = [scenario.id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate scenario ids")
    probe_ids = [probe.id for scenario in scenarios for probe in scenario.probes]
    if len(probe_ids) != len(set(probe_ids)):
        raise ValueError("Duplicate probe ids")
    for scenario in scenarios:
        validate_scenario(scenario)
    write_path = []
    for item in raw.get("write_path") or []:
        sessions = tuple(_strings(session, f"{item.get('id')}.sessions[]") for session in item.get("sessions") or [])
        if not sessions or not str(item.get("ask") or "").strip():
            raise ValueError(f"write-path probe {item.get('id')} needs sessions and ask")
        write_path.append(WritePathSpec(str(item["id"]), sessions, str(item["ask"]), _live(item.get("live"), item["id"])))
    return Fixture(1, str(raw.get("provenance") or ""), scenarios, tuple(write_path), hashlib.sha256(data).hexdigest())


__all__ = [
    "CATEGORIES",
    "FIXTURE",
    "Fixture",
    "LiveSpec",
    "NO_EVIDENCE_CATEGORIES",
    "RecordSpec",
    "ScenarioSpec",
    "SessionSpec",
    "TurnSpec",
    "WritePathSpec",
    "default_fragment",
    "load_fixture",
    "normalize",
]
