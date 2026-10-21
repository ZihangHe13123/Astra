"""Safety tests for the model-free Context Index shadow harness."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

from agent.evals.context_index_eval import (
    ShadowCase,
    _safe_opened,
    load_shadow_cases,
    main,
    run_shadow_cases,
)
from agent.runtime.context_index.broker import ContextIndexBroker
from agent.runtime.context_index.models import (
    ContextIndexPack,
    ContextIndexRow,
    ContextIndexTrace,
    EvidenceResult,
    RecommendationCandidate,
    SourceLocator,
    SourceResult,
)
from agent.runtime.context_index.workspace import WorkspaceIdentity


class _FakeBroker:
    """A real-shape broker double with deliberately private-looking input."""

    def __init__(self) -> None:
        self.last_trace: ContextIndexTrace | None = None
        self.calls: list[str] = []

    async def build(self, user_text, request_id, session_id, workspace, now, active):
        self.calls.append(user_text)
        row = ContextIndexRow(
            handle="ctx:s:dead",
            source="session",
            slot="S1",
            description="full historical message /Users/alice/private.txt",
            reason="related",
        )
        self.last_trace = ContextIndexTrace(
            request_id=request_id,
            session_id="raw-session-id",
            mode="shadow",
            workspace_label=workspace.label,
            displayed=[row],
            opened=[("ctx:s:dead", "opened")],
            source_status={"session": "available"},
            latency_ms=1.25,
            rendered_chars=0,
        )
        return ContextIndexPack(request_id=request_id, rendered="", rows=(row,))


CASE = ShadowCase(
    case_id="case-b",
    user_text="full historical message /Users/alice/private.txt",
    workspace_label="astra-master",
    workspace_key="/Users/alice/astra-master",
    local_time=datetime(2026, 8, 31, 8, 0, tzinfo=UTC),
    answer_variant="index_with_open",
    answer="Externally supplied comparison answer.",
    correction_next_turn=True,
    relevance_labels={"private-id": "relevant"},
)


def test_shadow_export_contains_only_sanitized_exposure_metadata(tmp_path: Path) -> None:
    output = tmp_path / "trace.jsonl"

    run_shadow_cases([CASE], broker=_FakeBroker(), output=output)

    record = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert set(record) == {
        "case_id",
        "mode",
        "workspace_label",
        "displayed",
        "opened",
        "source_status",
        "latency_ms",
        "rendered_chars",
        "correction_next_turn",
        "answer_variant",
        "answer",
    }
    assert record["displayed"] == [{"source": "session", "slot": "S1"}]
    assert record["opened"] == ["session"]
    serialized = json.dumps(record, ensure_ascii=False)
    assert "/Users/" not in serialized
    assert "full historical message" not in serialized
    assert "private-id" not in serialized
    assert "ctx:s:dead" not in serialized


def test_safe_opened_maps_real_broker_statuses_back_to_displayed_sources() -> None:
    class OpenableSession:
        def recommend(self, query, workspace, current_session_id, active_fingerprints, now, plan=None):
            del query, workspace, current_session_id, active_fingerprints, now
            return SourceResult(
                availability="available",
                relevance=(RecommendationCandidate(
                    source="session", identity="private", description="safe", timestamp=1.0,
                    workspace_tier=2, native_query_rank=0.0, topic_key="topic",
                    trust_label="historical_context", locator=SourceLocator("session_message", "private", 1),
                ),),
            )

        def open(self, locator, window, plan=None):
            del locator, window
            return EvidenceResult("session", "historical_context", "safe", ("safe",))

    class EmptyActivity:
        def recommend(self, query, workspace, now, plan=None):
            del query, workspace, now
            return SourceResult(availability="available")

        def open(self, locator, window, plan=None):
            del locator, window
            return EvidenceResult("activity", "untrusted_observation", "", ())

    # Not a test of the 200 ms source deadline, which can run out on a busy runner before a source thread starts.
    broker = ContextIndexBroker("session", 900, OpenableSession(), EmptyActivity(), source_deadline_seconds=5.0)
    pack = asyncio.run(broker.build(
        "continue", "real-trace", "session", WorkspaceIdentity("key", "", "fixture"), 1.0, frozenset()
    ))
    assert pack.rows
    assert broker.open([pack.rows[0].handle], 0).startswith("<context-evidence>")
    assert broker.last_trace is not None
    assert broker.last_trace.opened == [(pack.rows[0].handle, "opened")]
    assert _safe_opened(broker.last_trace, "index_with_open") == ("session",)


def test_shadow_cases_skip_malformed_records_and_sort_deterministically(tmp_path: Path) -> None:
    turns = tmp_path / "turns.jsonl"
    turns.write_text(
        "\n".join(
            [
                json.dumps({
                    "case_id": "z-last", "user_text": "continue", "workspace_label": "z",
                    "local_time": "2026-08-31T08:00:00+00:00", "answer_variant": "off",
                }),
                "not-json",
                json.dumps({
                    "case_id": "a-first", "user_text": "continue", "workspace_label": "a",
                    "local_time": "bad timestamp", "answer_variant": "index_no_open",
                }),
                json.dumps({
                    "case_id": "b-middle", "user_text": "continue", "workspace_label": "b",
                    "local_time": "2026-08-31T08:00:00+00:00", "answer_variant": "unknown",
                }),
                json.dumps({
                    "case_id": "a-first", "user_text": "valid", "workspace_label": "a",
                    "local_time": "2026-08-31T08:00:00+00:00", "answer_variant": "off",
                }),
            ]
        ),
        encoding="utf-8",
    )

    cases = load_shadow_cases(turns)

    assert [case.case_id for case in cases] == ["a-first", "z-last"]


def test_shadow_harness_never_derives_opens_or_answers_from_labels(tmp_path: Path) -> None:
    case = ShadowCase(
        case_id="labels-are-not-opens",
        user_text="continue",
        workspace_label="astra-master",
        workspace_key="safe-key",
        local_time=datetime(2026, 8, 31, 8, 0, tzinfo=UTC),
        answer_variant="index_no_open",
        answer="provided externally",
        relevance_labels={"some-source": "relevant"},
    )
    output = tmp_path / "trace.jsonl"

    run_shadow_cases([case], broker=_FakeBroker(), output=output)

    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["answer"] == "provided externally"
    assert record["answer_variant"] == "index_no_open"
    assert record["opened"] == []


def test_shadow_runner_uses_the_real_async_broker_boundary(tmp_path: Path) -> None:
    broker = _FakeBroker()
    output = tmp_path / "trace.jsonl"

    run_shadow_cases([CASE], broker=broker, output=output)

    assert broker.calls == [CASE.user_text]


def test_shadow_cli_skips_bad_input_and_never_exports_turn_text(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    turns = tmp_path / "turns.jsonl"
    output = tmp_path / "output.jsonl"
    turns.write_text(
        "\n".join(
            [
                "malformed",
                json.dumps(
                    {
                        "case_id": "cli-case",
                        "user_text": "private turn /Users/alice/secret.txt",
                        "workspace_label": "fixture",
                        "workspace_key": "private-key",
                        "local_time": "2026-08-31T08:00:00+00:00",
                        "answer_variant": "off",
                        "answer": "answer at /Users/alice/answer.txt",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    assert main(["--fixture", str(fixture), "--turns", str(turns), "--output", str(output)]) == 0

    serialized = output.read_text(encoding="utf-8")
    assert "private turn" not in serialized
    assert "private-key" not in serialized
    assert "/Users/alice/answer.txt" not in serialized
    assert json.loads(serialized)["answer"] == ""


def test_shadow_export_rejects_malicious_external_answer_instead_of_redacting_fragments(tmp_path: Path) -> None:
    case = ShadowCase(
        case_id="malicious-answer",
        user_text="continue",
        workspace_label="fixture",
        workspace_key="fixture-key",
        local_time=datetime(2026, 8, 31, 8, 0, tzinfo=UTC),
        answer_variant="off",
        answer=(
            "file:///Users/alice/private.txt "
            + chr(92) * 2
            + "server"
            + chr(92)
            + "share"
            + chr(92)
            + "secret.txt C:/Users/alice/secret.txt "
            + "https://example.test/?path=%2FUsers%2Falice \u202econtrol"
        ),
    )
    output = tmp_path / "trace.jsonl"

    run_shadow_cases([case], broker=_FakeBroker(), output=output)

    record = json.loads(output.read_text(encoding="utf-8"))
    assert record["answer"] == ""
