from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import UTC, datetime

import pytest

from agent.runtime.context_index.broker import ContextIndexBroker
from agent.runtime.context_index.models import (
    ContextIndexRow,
    EvidenceResult,
    RecommendationCandidate,
    SourceLocator,
    SourceResult,
)
from agent.runtime.context_index.workspace import WorkspaceIdentity

NOW = datetime(2026, 8, 31, 9, 5, tzinfo=UTC)
WORKSPACE = WorkspaceIdentity("workspace", "/private/workspace", "astra-master")


def _candidate(source: str, suffix: str, description: str | None = None) -> RecommendationCandidate:
    trust = {
        "session": "historical_context",
        "activity": "untrusted_observation",
        "habit": "inferred_pattern",
    }[source]
    kind = {
        "session": "session_message",
        "activity": "activity_event",
        "habit": "habit",
    }[source]
    return RecommendationCandidate(
        source=source,  # type: ignore[arg-type]
        identity=f"identity-{suffix}",
        description=description or f"description {suffix}",
        timestamp=NOW.timestamp(),
        workspace_tier=0,
        native_query_rank=1.0,
        topic_key=f"topic-{suffix}",
        trust_label=trust,  # type: ignore[arg-type]
        locator=SourceLocator(kind, f"private-{suffix}", 1),  # type: ignore[arg-type]
        project_label="astra-master",
        cache_state="fresh-cache" if source != "session" else "",
        support_days=4 if source == "habit" else 0,
        support_weeks=3 if source == "habit" else 0,
        reason="inferred" if source == "habit" else "",
    )


class FakeSource:
    def __init__(
        self,
        source: str,
        *,
        delay: float = 0.0,
        error: BaseException | None = None,
        result: SourceResult | None = None,
    ) -> None:
        self.source = source
        self.delay = delay
        self.error = error
        self.calls = 0
        self.open_calls = 0
        self.last_window: int | None = None
        candidate = _candidate(source, source)
        self.result = result or SourceResult(
            availability="available",
            relevance=(candidate,) if source != "habit" else (),
            recency=(
                _candidate(source, f"{source}-recent"),
            ) if source != "habit" else (),
            habit=candidate if source == "habit" else None,
            cache_state="fresh-cache" if source == "activity" else "",
        )

    def recommend(self, *args: object) -> SourceResult:
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result

    def open(self, locator: SourceLocator, window: int, plan=None) -> EvidenceResult:
        self.open_calls += 1
        self.last_window = window
        if self.error is not None:
            raise self.error
        trust = "historical_context" if self.source == "session" else "untrusted_observation"
        return EvidenceResult(
            source=self.source,  # type: ignore[arg-type]
            trust_label=trust,  # type: ignore[arg-type]
            title=f"{self.source} evidence",
            items=("safe evidence",),
        )


class ReplacementRaceSource(FakeSource):
    def __init__(self, *, old_error: bool = False) -> None:
        super().__init__("session")
        self.old_error = old_error
        self.old_started = threading.Event()
        self.old_release = threading.Event()
        self.new_started = threading.Event()

    def recommend(self, *args: object) -> SourceResult:
        self.calls += 1
        query = str(args[0])
        if query == "old":
            self.old_started.set()
            assert self.old_release.wait(1.0)
            if self.old_error:
                raise RuntimeError("private old failure")
        else:
            self.new_started.set()
        return self.result


def make_broker(
    *,
    mode: str = "all",
    session: FakeSource | None = None,
    activity: FakeSource | None = None,
    source_deadline_seconds: float | None = None,
) -> ContextIndexBroker:
    return ContextIndexBroker(
        mode=mode,
        char_budget=900,
        session_source=session or FakeSource("session"),
        activity_source=activity or FakeSource("activity"),
        source_deadline_seconds=source_deadline_seconds,
    )


async def _build(broker: ContextIndexBroker, request_id: str = "req-1", text: str = "continue"):
    return await broker.build(
        text,
        request_id,
        "session-current",
        WORKSPACE,
        NOW,
        frozenset(),
    )


async def _event(event: threading.Event) -> None:
    for _ in range(100):
        if event.is_set():
            return
        await asyncio.sleep(0.002)
    raise AssertionError("timed out waiting for race event")


def test_build_is_once_per_request_and_byte_stable() -> None:
    broker = make_broker()
    first = asyncio.run(_build(broker))
    second = asyncio.run(_build(broker, text="changed input"))

    assert first is second
    assert first.rendered == second.rendered
    assert broker.session_source.calls == broker.activity_source.calls == 1


def test_feedback_impression_is_once_bounded_and_content_free() -> None:
    events: list[dict] = []
    broker = make_broker()
    broker.set_feedback_sink(events.append)

    first = asyncio.run(_build(broker))
    second = asyncio.run(_build(broker, text="private changed query"))

    assert first is second
    assert len(events) == 1
    event = events[0]
    assert set(event) == {"type", "schema_version", "request_id", "mode", "rows", "diagnostics"}
    assert event["type"] == "context_index_built"
    assert event["schema_version"] == 3
    assert event["request_id"] == "req-1"
    assert event["mode"] == "all"
    assert len(event["rows"]) == len(first.rows) <= 6
    assert all(
        set(row) == {
            "handle", "source", "slot", "reason", "position", "cache_state"
        }
        for row in event["rows"]
    )
    serialized = json.dumps(event)
    for private_value in (
        "private changed query",
        "description session",
        "astra-master",
        "private-session",
        "safe evidence",
    ):
        assert private_value not in serialized


def test_feedback_impression_is_not_emitted_for_off_or_shadow() -> None:
    events: list[dict] = []
    broker = make_broker(mode="off")
    broker.set_feedback_sink(events.append)

    assert asyncio.run(_build(broker, "off")).rows == ()
    broker.set_mode("shadow")
    assert asyncio.run(_build(broker, "shadow")).rows

    assert events == []


def test_feedback_open_records_only_validated_bounded_outcomes() -> None:
    events: list[dict] = []
    session = FakeSource("session")
    activity = FakeSource("activity")
    broker = make_broker(session=session, activity=activity)
    broker.set_feedback_sink(events.append)
    pack = asyncio.run(_build(broker))
    events.clear()
    handles = [
        next(row.handle for row in pack.rows if row.source == "session"),
        next(row.handle for row in pack.rows if row.source == "activity"),
    ]
    activity.error = RuntimeError("private evidence failure")

    result = broker.open(handles, 99)

    assert "historical_context" in result
    assert events == [{
        "type": "context_index_open",
        "schema_version": 1,
        "request_id": "req-1",
        "window": 5,
        "outcomes": [
            {"handle": handles[0], "status": "opened"},
            {"handle": handles[1], "status": "evidence_unavailable"},
        ],
    }]

    events.clear()
    assert broker.open(["ctx:s:ffff"], 2) == "invalid_or_expired_handle"
    assert events == []
    broker.complete_request("req-1")
    assert broker.open([handles[0]], 2) == "invalid_or_expired_handle"
    assert events == []


def test_feedback_sink_failure_does_not_change_build_or_open(caplog) -> None:
    broker = make_broker(mode="session")

    def explode(_event):
        raise RuntimeError("private sink detail")

    broker.set_feedback_sink(explode)

    pack = asyncio.run(_build(broker))
    opened = broker.open([pack.rows[0].handle], 2)

    assert pack.rows
    assert "safe evidence" in opened
    assert "context index feedback sink failed" in caplog.text
    assert "private sink detail" not in caplog.text


def test_feedback_row_replaces_non_allowlisted_values() -> None:
    row = ContextIndexRow(
        handle="private-handle",
        source="private",  # type: ignore[arg-type]
        slot="X9",
        description="private description",
        reason="private reason",
        project_label="private project",
        cache_state="fresh-cache",
        timestamp=123.0,
    )

    assert ContextIndexBroker._feedback_row(row, 99) == {
        "handle": "invalid-handle",
        "source": "session",
        "slot": "S1",
        "reason": "selected",
        "position": 6,
        "cache_state": "",
    }


def test_feedback_open_limit_rejection_adds_no_outcome() -> None:
    events: list[dict] = []
    broker = make_broker(mode="session")
    broker.set_feedback_sink(events.append)
    pack = asyncio.run(_build(broker))
    handle = pack.rows[0].handle
    events.clear()

    assert "safe evidence" in broker.open([handle], 2)
    assert "safe evidence" in broker.open([handle], 2)
    assert len(events) == 2
    assert broker.open([handle], 2) == "open_limit_reached"
    assert len(events) == 2


def test_optional_legacy_recency_omission_keeps_activity_available_in_trace() -> None:
    activity = FakeSource(
        "activity",
        result=SourceResult(
            availability="available",
            relevance=(_candidate("activity", "fts"),),
            error_category="recency_omitted",
            diagnostics=("recency_omitted", "habit_omitted"),
        ),
    )
    broker = make_broker(activity=activity)

    pack = asyncio.run(_build(broker))

    assert any(row.source == "activity" for row in pack.rows)
    assert broker.last_trace is not None
    assert broker.last_trace.source_status["activity"] == "available"
    assert broker.last_trace.source_errors["activity"] == "recency_omitted"
    assert broker.last_trace.source_diagnostics["activity"] == (
        "recency_omitted", "habit_omitted"
    )


def test_session_channel_omission_stays_available_and_visible_in_trace() -> None:
    session = FakeSource(
        "session",
        result=SourceResult(
            availability="available",
            relevance=(_candidate("session", "kept"),),
            error_category="deadline",
            diagnostics=("recency_omitted",),
        ),
    )
    broker = make_broker(mode="session", session=session)

    pack = asyncio.run(_build(broker))
    formatted = broker.format_last_trace()

    assert pack.rows
    assert broker.last_trace is not None
    assert broker.last_trace.source_status["session"] == "available"
    assert broker.last_trace.source_diagnostics["session"] == ("recency_omitted",)
    assert "Source session: available (deadline)" in formatted
    assert "Diagnostics session: recency_omitted" in formatted
    assert "interrupted" not in formatted and "SELECT" not in formatted


def test_trace_diagnostics_are_allowlisted_when_rendered() -> None:
    broker = make_broker(mode="session")
    asyncio.run(_build(broker))
    assert broker.last_trace is not None
    broker.last_trace.source_diagnostics["session"] = (
        "OperationalError: SELECT * FROM hidden /private/db",
        "recency_omitted",
    )

    formatted = broker.format_last_trace()

    assert "Diagnostics session: recency_omitted" in formatted
    assert "OperationalError" not in formatted
    assert "SELECT" not in formatted
    assert "/private" not in formatted


def test_concurrent_builds_share_one_source_generation() -> None:
    session = FakeSource("session", delay=0.02)
    activity = FakeSource("activity", delay=0.02)
    broker = make_broker(session=session, activity=activity)

    async def run() -> tuple[object, object]:
        return tuple(await asyncio.gather(_build(broker), _build(broker, text="changed")))  # type: ignore[return-value]

    first, second = asyncio.run(run())
    assert first is second
    assert session.calls == activity.calls == 1


def test_cancelled_build_cannot_publish_late_handles_or_pack() -> None:
    session = FakeSource("session", delay=0.05)
    broker = make_broker(mode="session", session=session)

    async def cancel_then_retry():
        pending = asyncio.create_task(_build(broker))
        await asyncio.sleep(0.005)
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        await asyncio.sleep(0.06)
        return await _build(broker)

    pack = asyncio.run(cancel_then_retry())

    assert pack.rows
    assert session.calls == 2


def test_mode_change_during_build_returns_empty_without_stale_state() -> None:
    session = FakeSource("session", delay=0.05)
    broker = make_broker(mode="session", session=session)

    async def change_mode():
        pending = asyncio.create_task(_build(broker))
        await asyncio.sleep(0.005)
        broker.set_mode("off")
        return await pending

    pack = asyncio.run(change_mode())

    assert pack.rendered == "" and pack.rows == ()
    assert broker.last_trace is None


def test_complete_request_during_build_cannot_publish_late_state() -> None:
    session = FakeSource("session", delay=0.05)
    # What is checked is the request ending during a build, not the source deadline: give a slow
    # runner room so the retry's own 50 ms source cannot time out.
    broker = make_broker(mode="session", session=session, source_deadline_seconds=5.0)

    async def complete_then_retry():
        pending = asyncio.create_task(_build(broker))
        await asyncio.sleep(0.005)
        broker.complete_request("req-1")
        first = await pending
        retry = await _build(broker)
        return first, retry

    first, retry = asyncio.run(complete_then_retry())

    assert first.rendered == "" and first.rows == ()
    assert retry.rows
    assert session.calls == 2


def test_old_caller_cancellation_cannot_expire_replacement_generation() -> None:
    session = ReplacementRaceSource()
    broker = make_broker(mode="session", session=session)

    async def replace_then_cancel_old():
        old = asyncio.create_task(_build(broker, text="old"))
        await _event(session.old_started)
        broker.complete_request("req-1")
        new = asyncio.create_task(_build(broker, text="new"))
        await _event(session.new_started)
        replacement = await new
        old.cancel()
        with pytest.raises(asyncio.CancelledError):
            await old
        session.old_release.set()
        await asyncio.sleep(0.01)
        cached = await _build(broker, text="changed")
        return replacement, cached

    replacement, cached = asyncio.run(replace_then_cancel_old())

    assert cached is replacement
    assert session.calls == 2
    assert broker.open([replacement.rows[0].handle], 2) != "invalid_or_expired_handle"


@pytest.mark.parametrize("old_error", [False, True])
def test_old_late_result_or_exception_cannot_replace_new_pack(old_error: bool) -> None:
    session = ReplacementRaceSource(old_error=old_error)
    broker = make_broker(mode="session", session=session)

    async def replace_then_finish_old():
        old = asyncio.create_task(_build(broker, text="old"))
        await _event(session.old_started)
        broker.complete_request("req-1")
        replacement = await _build(broker, text="new")
        session.old_release.set()
        stale = await old
        cached = await _build(broker, text="changed")
        return replacement, stale, cached

    replacement, stale, cached = asyncio.run(replace_then_finish_old())

    assert stale.rendered == "" and stale.rows == ()
    assert cached is replacement
    assert broker.open([replacement.rows[0].handle], 2) != "invalid_or_expired_handle"


def test_off_opens_no_source_and_shadow_selects_without_injecting() -> None:
    session = FakeSource("session", error=AssertionError("must not call"))
    activity = FakeSource("activity", error=AssertionError("must not call"))
    broker = make_broker(mode="off", session=session, activity=activity)

    assert asyncio.run(_build(broker, "off")).rendered == ""
    assert session.calls == activity.calls == 0
    assert broker.last_trace is None

    session.error = activity.error = None
    broker.set_mode("shadow")
    shadow = asyncio.run(_build(broker, "shadow"))
    assert shadow.rendered == ""
    assert shadow.rows
    assert broker.last_trace is not None and broker.last_trace.displayed
    assert broker.open([shadow.rows[0].handle], 2) == "invalid_or_expired_handle"


def test_session_mode_never_starts_activity_source() -> None:
    activity = FakeSource("activity", error=AssertionError("must not call"))
    broker = make_broker(mode="session", activity=activity)

    pack = asyncio.run(_build(broker))

    assert "session" in pack.rendered
    assert activity.calls == 0


def test_one_slow_source_does_not_block_completed_source() -> None:
    # delay 跟随预算常量：任何 deadline 调整都不会把这个契约测试变哑炮。
    from agent.runtime.context_index.broker import _SOURCE_DEADLINE_SECONDS

    session = FakeSource("session", delay=_SOURCE_DEADLINE_SECONDS + 0.06)
    activity = FakeSource("activity")
    broker = make_broker(session=session, activity=activity)

    pack = asyncio.run(_build(broker))

    assert "activity" in pack.rendered
    assert broker.last_trace is not None
    assert broker.last_trace.source_errors["session"] == "source_timeout"


def test_source_exception_is_bounded_and_other_source_survives() -> None:
    session = FakeSource("session", error=RuntimeError("SELECT secret FROM /private/db"))
    broker = make_broker(session=session)

    pack = asyncio.run(_build(broker))
    formatted = broker.format_last_trace()

    assert "activity" in pack.rendered
    assert "source_error" in formatted
    assert "SELECT" not in formatted
    assert "/private" not in formatted


def test_cache_evicts_oldest_pack_and_its_handles() -> None:
    broker = make_broker(mode="session")
    first = asyncio.run(_build(broker, "req-0"))
    old_handle = first.rows[0].handle
    for index in range(1, 9):
        asyncio.run(_build(broker, f"req-{index}"))

    assert broker.open([old_handle], 2) == "invalid_or_expired_handle"
    rebuilt = asyncio.run(_build(broker, "req-0"))
    assert rebuilt is not first


def test_handles_are_reused_for_cached_pack_and_open_current_evidence() -> None:
    session = FakeSource("session")
    broker = make_broker(mode="session", session=session)
    first = asyncio.run(_build(broker))
    second = asyncio.run(_build(broker, text="changed"))

    assert [row.handle for row in first.rows] == [row.handle for row in second.rows]
    opened = broker.open([first.rows[0].handle], 99)
    assert "historical_context" in opened
    assert "safe evidence" in opened
    assert session.last_window == 5


def test_replacement_generation_keeps_old_handles_expired_and_new_handles_live() -> None:
    broker = make_broker(mode="session")
    old = asyncio.run(_build(broker, text="old"))
    old_handle = old.rows[0].handle
    broker.complete_request("req-1")

    replacement = asyncio.run(_build(broker, text="new"))

    assert broker.open([old_handle], 2) == "invalid_or_expired_handle"
    assert broker.open([replacement.rows[0].handle], 2) != "invalid_or_expired_handle"


def test_open_rejects_malformed_duplicate_foreign_and_noncurrent_handles() -> None:
    broker = make_broker(mode="session")
    first = asyncio.run(_build(broker, "req-1"))
    first_handle = first.rows[0].handle
    second = asyncio.run(_build(broker, "req-2"))
    second_handle = second.rows[0].handle

    assert broker.open([], 2) == "invalid_request"
    assert broker.open([second_handle, second_handle], 2) == "invalid_request"
    assert broker.open([first_handle], 2) == "invalid_or_expired_handle"
    assert broker.open([first_handle, second_handle], 2) == "invalid_or_expired_handle"


def test_open_exception_is_fail_open_and_never_formats_exception() -> None:
    session = FakeSource("session")
    broker = make_broker(mode="session", session=session)
    pack = asyncio.run(_build(broker))
    session.error = RuntimeError("/private/db SELECT password")

    output = broker.open([pack.rows[0].handle], 2)

    assert output == "evidence_unavailable"
    assert "/private" not in broker.format_last_trace()
    assert "SELECT" not in broker.format_last_trace()


def test_mode_and_request_lifecycle_invalidate_state() -> None:
    broker = make_broker(mode="session")
    pack = asyncio.run(_build(broker))
    handle = pack.rows[0].handle

    with pytest.raises(ValueError):
        broker.set_mode("surprise")
    assert broker.open([handle], 2) != "invalid_or_expired_handle"

    broker.set_mode("all")
    assert broker.last_trace is None
    assert broker.open([handle], 2) == "invalid_or_expired_handle"

    next_pack = asyncio.run(_build(broker, "req-next"))
    broker.complete_request("req-next")
    assert broker.open([next_pack.rows[0].handle], 2) == "invalid_or_expired_handle"

    final_pack = asyncio.run(_build(broker, "req-final"))
    broker.end_session()
    assert broker.open([final_pack.rows[0].handle], 2) == "invalid_or_expired_handle"


def test_format_last_trace_is_bounded_allowlisted_and_empty_is_exact() -> None:
    broker = make_broker(mode="session")
    assert broker.format_last_trace() == "No Context Index decision has been recorded."

    asyncio.run(_build(broker))
    trace = broker.last_trace
    assert trace is not None
    trace.workspace_label = "../../private/secret"
    trace.displayed[0] = type(trace.displayed[0])(
        **{
            **trace.displayed[0].__dict__,
            "reason": "SELECT * FROM hidden /private/db",
        }
    )
    formatted = broker.format_last_trace()

    assert len(formatted) <= 4000
    assert "SELECT" not in formatted
    assert "/private" not in formatted
    assert "locator" not in formatted


def test_total_broker_exception_returns_empty_pack() -> None:
    broker = make_broker()
    broker._select_rows = lambda *_args: (_ for _ in ()).throw(RuntimeError("secret"))

    pack = asyncio.run(_build(broker))

    assert pack.rendered == "" and pack.rows == ()
    assert broker.last_trace is not None
    assert broker.last_trace.source_errors == {"broker": "broker_error"}
