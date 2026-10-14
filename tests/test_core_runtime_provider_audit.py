import argparse
import asyncio
import json
import math
import multiprocessing
import os
import signal
import struct
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent.runtime.react as react_runtime
import scripts.core_runtime_audit_files as audit_files
import scripts.core_runtime_audit_results as audit_results
import scripts.core_runtime_audit_worker as audit_worker
import scripts.core_runtime_provider_audit as audit
from agent.runtime.llm import LLMIdleTimeout
from agent.runtime.process_env import pid_alive
from scripts.core_runtime_provider_audit import probes_pass, redact_error, run_audit

requires_posix_audit_files = pytest.mark.skipif(
    os.name != "posix", reason="provider-audit files require POSIX descriptor-relative APIs"
)


def test_audit_files_reject_platform_without_safe_primitives(monkeypatch):
    monkeypatch.setattr(audit_files.os, "name", "nt")
    with pytest.raises(RuntimeError, match="descriptor-relative"):
        audit_files._require_safe_primitives()


def _worker_after_ready(ready, target, args):
    ready.set()
    target(*args)


@pytest.fixture
def ready_audit_worker(monkeypatch):
    """Wire-format tests start their deadline after the child imports finish."""
    make_process = audit_worker._make_process

    class ReadyProcess:
        def __init__(self, process, ready):
            self.process = process
            self.ready = ready

        def __getattr__(self, name):
            return getattr(self.process, name)

        def start(self):
            self.process.start()
            if not self.ready.wait(timeout=10):
                raise RuntimeError("wire-format worker did not become ready")

    def make_ready_process(context, *, target, args, name):
        ready = context.Event()
        process = make_process(
            context, target=_worker_after_ready, args=(ready, target, args), name=name,
        )
        return ReadyProcess(process, ready)

    monkeypatch.setattr(audit_worker, "_make_process", make_ready_process)


def _stubborn_worker(connection, probe_id, payload):
    del connection, probe_id
    if os.name != "nt":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    Path(payload["ready_path"]).write_text(json.dumps({"ready": True, "pid": os.getpid()}), encoding="utf-8")
    while True:
        time.sleep(0.1)


def _hostile_worker(connection, probe_id, payload):
    del payload
    audit_worker.send_worker_result(
        connection,
        {
            "id": probe_id,
            "status": "passed",
            "duration_ms": 1,
            "error_type": "api_key=ipc-secret",
            "secret_field": "Authorization: Bearer ipc-secret",
        },
    )
    connection.close()


def _malformed_worker(connection, probe_id, payload):
    del probe_id, payload
    connection.send_bytes(b'["api_key=ipc-secret"]')
    connection.close()


def _unexpected_exit_worker(connection, probe_id, payload):
    del probe_id, payload
    connection.close()


@pytest.mark.parametrize("winerror, expected", [(109, "ProbeWorkerExit"), (5, "ProbeIPCFailure")])
def test_exited_worker_named_pipe_poll_errors_are_classified(monkeypatch, winerror, expected):
    closed = []
    error = OSError("named pipe unavailable")
    error.winerror = winerror

    def poll():
        raise error

    receive = SimpleNamespace(poll=poll, close=lambda: closed.append("receive"))
    send = SimpleNamespace(close=lambda: closed.append("send"))
    context = SimpleNamespace(Pipe=lambda **_kwargs: (receive, send))
    process = SimpleNamespace(
        pid=None, start=lambda: None, is_alive=lambda: False, join=lambda: None,
    )
    monkeypatch.setattr(audit_worker, "multiprocessing", SimpleNamespace(get_context=lambda _mode: context))
    monkeypatch.setattr(audit_worker, "_make_process", lambda *_args, **_kwargs: process)

    result = asyncio.run(audit_worker.run_probe_worker(
        "LIVE-01", {}, 1, worker_target=_unexpected_exit_worker,
    ))

    assert result["status"] == "product_failure"
    assert result["error_type"] == expected
    assert "receive" in closed and "send" in closed


def _payload_worker(connection, probe_id, payload):
    del probe_id
    audit_worker.send_worker_result(connection, payload["result"])
    connection.close()


def _non_json_worker(connection, probe_id, payload):
    del probe_id, payload
    connection.send_bytes(b"not-json")
    connection.close()


def _oversize_wire_worker(connection, probe_id, payload):
    del probe_id, payload
    connection.send_bytes(b"x" * (audit_worker.MAX_WORKER_RESULT_BYTES + 1))
    connection.close()


def _partial_wire_worker(connection, probe_id, payload):
    del probe_id
    time.sleep(payload.get("startup_delay", 0))
    os.write(connection.fileno(), struct.pack("!i", 32) + b"{")
    Path(payload["ready_path"]).write_text("ready", encoding="utf-8")
    side_effect = Path(payload["side_effect_path"])
    while True:
        side_effect.write_text(str(time.monotonic()), encoding="utf-8")
        time.sleep(0.01)


def _write_unpickle_side_effect(path):
    Path(path).write_text("parent-unpickled", encoding="utf-8")
    return {"status": "passed"}


class _EvilPicklePayload:
    def __init__(self, path):
        self.path = path

    def __reduce__(self):
        return (_write_unpickle_side_effect, (self.path,))


def _evil_pickle_worker(connection, probe_id, payload):
    del probe_id
    connection.send(_EvilPicklePayload(payload["side_effect_path"]))
    connection.close()


def test_redact_error_removes_bearer_and_api_key():
    redacted = redact_error("Authorization: Bearer secret api_key=abc123")

    assert "secret" not in redacted
    assert "abc123" not in redacted


def test_two_of_three_completed_probes_pass():
    probes = [
        {"status": "passed"},
        {"status": "passed"},
        {"status": "external_failure"},
    ]

    assert probes_pass(probes) is True


@pytest.mark.parametrize("product_failure_index", [0, 1, 2])
def test_product_failure_prevents_audit_pass(product_failure_index):
    probes = [{"status": "passed"} for _ in range(3)]
    probes[product_failure_index] = {"status": "product_failure"}

    assert probes_pass(probes) is False


@requires_posix_audit_files
def test_audit_output_rejects_symlink_before_chmod_or_write(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    state_dir = project_root / ".astra"
    state_dir.mkdir(parents=True)
    output_link = state_dir / "core-runtime-audit"
    output_link.symlink_to("..", target_is_directory=True)
    chmod_calls = []

    monkeypatch.setattr(audit, "PROJECT_ROOT", project_root)
    monkeypatch.setattr(
        Path,
        "chmod",
        lambda self, mode: chmod_calls.append((self, mode)),
    )

    with pytest.raises(RuntimeError, match="symlink"):
        audit._audit_output_dir()

    assert chmod_calls == []
    assert not (project_root / "probe-input.txt").exists()


class RateLimitError(Exception):
    pass


class FakeClient:
    def __init__(self, *, normal: str = "passed"):
        self.normal = normal
        self.calls = 0
        self.cancelled = False
        self.close_calls = 0

    async def aclose(self):
        self.close_calls += 1

    async def chat_stream(self, messages, tools):
        del messages, tools
        self.calls += 1
        if self.calls == 1:
            if self.normal == "external_failure":
                raise RateLimitError(
                    "Authorization: Bearer provider-secret api_key=provider-key"
                )
            if self.normal == "product_failure":
                yield {
                    "type": "done",
                    "content": "",
                    "finish_reason": "stop",
                    "usage": None,
                }
                return
            yield {"type": "chunk", "content": "FULL_PROVIDER_TEXT"}
            yield {
                "type": "done",
                "content": "FULL_PROVIDER_TEXT",
                "finish_reason": "stop",
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "total_tokens": 6,
                },
            }
            return
        if self.calls == 2:
            yield {"type": "chunk", "content": "CANCELLATION_PROVIDER_TEXT"}
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return
        if self.calls == 3:
            yield {"type": "chunk", "content": "HEALTH_PROVIDER_TEXT"}
            yield {
                "type": "done",
                "content": "HEALTH_PROVIDER_TEXT",
                "finish_reason": "stop",
                "usage": None,
            }
            return
        raise AssertionError("unexpected fake provider request")


class FakeReadAgent:
    def __init__(self, output_dir: Path, *, wrong_tool: bool = False):
        self.output_dir = output_dir
        self.wrong_tool = wrong_tool

    async def reply_stream(self, message):
        del message
        marker = (self.output_dir / "probe-input.txt").read_text(encoding="utf-8")
        name = "write_file" if self.wrong_tool else "read_file"
        yield {
            "type": "tool_calls",
            "calls": [{"id": "call-1", "name": name, "arguments": "{}"}],
        }
        yield {
            "type": "tool_result",
            "name": name,
            "output": marker,
            "error": "",
        }
        yield {"type": "chunk", "content": "FULL_AGENT_TEXT"}
        yield {"type": "done"}


def _args() -> argparse.Namespace:
    return argparse.Namespace(
        model_key="fake::model",
        discovery_timeout=0.01,
        idle_timeout=1.0,
    )


def _entry():
    profile = SimpleNamespace(
        provider="fake",
        api_key=lambda: "provider-key",
        capabilities=frozenset(),
        temperature=0.1,
        max_tokens=128,
        top_p=None,
        top_k=None,
        min_p=None,
        presence_penalty=None,
        repetition_penalty=None,
        repetition_penalty_parameter=None,
    )
    return SimpleNamespace(
        key="fake::model",
        model_id="fake-model",
        base_url="https://provider.invalid",
        profile=profile,
    )


def _install_fakes(monkeypatch, tmp_path, *, normal="passed", wrong_tool=False):
    client = FakeClient(normal=normal)

    async def resolve_entry(args):
        assert args.model_key == "fake::model"
        return _entry()

    monkeypatch.setattr(audit, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(audit, "_resolve_entry", resolve_entry)
    monkeypatch.setattr(audit, "_build_client", lambda entry, args: client)
    monkeypatch.setattr(
        audit,
        "_build_read_agent",
        lambda client, audit_run, marker_identity, args: FakeReadAgent(
            audit_run.path, wrong_tool=wrong_tool
        ),
    )
    monkeypatch.setattr(audit, "load_project_env", lambda project_root: None)

    async def run_fake_worker(probe_id, payload, timeout, **kwargs):
        del timeout, kwargs
        try:
            with audit_files.open_audit_run(
                Path(payload["project_root"]),
                payload["run_name"],
                device=payload["run_device"],
                inode=payload["run_inode"],
            ) as audit_run:
                return await audit._probe_operation(
                    probe_id,
                    payload["entry"],
                    argparse.Namespace(**payload["args"]),
                    audit_run,
                    payload["marker"],
                    tuple(payload["marker_identity"]),
                )
        except Exception as exc:
            return audit._failure_probe(probe_id, exc, 0)

    monkeypatch.setattr(audit, "_run_probe_worker", run_fake_worker)
    return client


@requires_posix_audit_files
def test_run_audit_orchestrates_exactly_three_private_offline_probes(
    tmp_path, monkeypatch
):
    client = _install_fakes(monkeypatch, tmp_path)
    bounded_calls = []
    original = audit._run_probe_worker

    async def record_bound(probe_id, payload, timeout, **kwargs):
        bounded_calls.append((probe_id, timeout))
        return await original(probe_id, payload, timeout, **kwargs)

    monkeypatch.setattr(audit, "_run_probe_worker", record_bound)

    report = asyncio.run(run_audit(_args()))

    assert report["passed"] is True
    assert [probe["id"] for probe in report["probes"]] == [
        "LIVE-01",
        "LIVE-02",
        "LIVE-03",
    ]
    assert [probe["status"] for probe in report["probes"]] == [
        "passed",
        "passed",
        "passed",
    ]
    assert bounded_calls == [
        ("LIVE-01", 180.0),
        ("LIVE-02", 180.0),
        ("LIVE-03", 180.0),
    ]
    assert client.calls == 3
    assert client.cancelled is True
    assert client.close_calls == 3
    assert report["probes"][0]["final_text_length"] == len("FULL_PROVIDER_TEXT")
    assert report["probes"][0]["usage_totals"] == {
        "prompt_tokens": 4,
        "completion_tokens": 2,
        "total_tokens": 6,
    }
    assert report["probes"][1]["tool_names"] == ["read_file"]
    assert report["probes"][1]["result_status"] == "succeeded"
    assert report["probes"][1]["marker_observed"] is True
    assert report["probes"][2]["cancellation_completed"] is True
    assert report["probes"][2]["health_request_succeeded"] is True

    report_path = Path(report["report_path"])
    output_root = tmp_path / ".astra" / "core-runtime-audit"
    output_dir = report_path.parent
    assert output_dir.parent == output_root
    assert output_dir.name.startswith("run-")
    assert sorted(path.name for path in output_dir.iterdir()) == sorted(
        [
            ".astra",
            "image-cache",
            "probe-input.txt",
            report_path.name,
            "tool-results",
        ]
    )
    persisted = report_path.read_text(encoding="utf-8")
    assert json.loads(persisted) == report
    assert "FULL_PROVIDER_TEXT" not in persisted
    assert "FULL_AGENT_TEXT" not in persisted
    assert (output_dir / "probe-input.txt").read_text(encoding="utf-8") not in persisted
    if os.name != "nt":
        assert report_path.stat().st_mode & 0o777 == 0o600
        assert (output_dir / "probe-input.txt").stat().st_mode & 0o777 == 0o600


@requires_posix_audit_files
def test_external_provider_failure_is_redacted_and_two_of_three_still_pass(
    tmp_path, monkeypatch
):
    _install_fakes(monkeypatch, tmp_path, normal="external_failure")

    report = asyncio.run(run_audit(_args()))

    assert report["passed"] is True
    assert report["probes"][0]["status"] == "external_failure"
    assert report["probes"][0]["error_type"] == "RateLimitError"
    persisted = Path(report["report_path"]).read_text(encoding="utf-8")
    assert "provider-secret" not in persisted
    assert "provider-key" not in persisted
    assert "Authorization" not in persisted


@requires_posix_audit_files
def test_two_product_invariant_failures_fail_the_audit(tmp_path, monkeypatch):
    _install_fakes(
        monkeypatch,
        tmp_path,
        normal="product_failure",
        wrong_tool=True,
    )

    report = asyncio.run(run_audit(_args()))

    assert report["passed"] is False
    assert [probe["status"] for probe in report["probes"]] == [
        "product_failure",
        "product_failure",
        "passed",
    ]


@pytest.mark.parametrize(
    "value",
    [
        "authorization: bearer top-secret",
        "API-KEY: top-secret",
        '"api_key": "top-secret"',
        "token=top-secret",
        "password: top-secret",
    ],
)
def test_redact_error_covers_common_secret_labels(value):
    assert "top-secret" not in redact_error(value)


@pytest.mark.parametrize(
    "value",
    [
        '{"headers":{"Authorization":"Bearer nested-secret"}}',
        "{'Authorization': 'Bearer nested-secret'}",
        '{"api_key":"nested-secret"}',
        "{'API-KEY': 'nested-secret'}",
    ],
)
def test_redact_error_covers_quoted_and_nested_secret_fields(value):
    assert "nested-secret" not in redact_error(value)


def test_model_key_is_redacted_identifier_safe_and_bounded():
    raw = (
        'provider::model {"Authorization":"Bearer model-secret",'
        '"api_key":"model-key"}\n' + "x" * 500
    )

    safe = audit._safe_model_key(raw)

    assert "model-secret" not in safe
    assert "model-key" not in safe
    assert "\n" not in safe
    assert len(safe) <= 120


def test_live_01_reasoning_only_stream_is_not_visible_success(monkeypatch):
    class ReasoningOnlyClient:
        async def chat_stream(self, messages, tools):
            del messages, tools
            yield {"type": "reasoning", "content": "private chain of thought"}
            yield {
                "type": "done",
                "content": "",
                "finish_reason": "stop",
                "usage": None,
            }

    monkeypatch.setattr(
        audit,
        "_build_client",
        lambda entry, args: ReasoningOnlyClient(),
    )

    result = asyncio.run(audit._normal_stream_probe(_entry(), _args()))

    assert result["status"] == "product_failure"
    assert result["final_text_length"] == 0


class ReadToolLLM:
    class _Config:
        model = "fake-read-model"
        capabilities = frozenset()

    config = _Config()

    def __init__(self):
        self.calls = 0
        self.exposed_tools = []
        self.message_batches = []

    async def chat_stream(self, messages, tools):
        self.message_batches.append(messages)
        self.calls += 1
        self.exposed_tools = [
            item["function"]["name"] for item in (tools or [])
        ]
        yield {
            "type": "tool_calls",
            "calls": [
                {
                    "id": "read-1",
                    "name": "read_file",
                    "arguments": '{"path":"probe-input.txt"}',
                }
            ],
            "content": "",
            "reasoning_content": "",
            "finish_reason": "tool_calls",
            "usage": None,
        }


@requires_posix_audit_files
def test_read_agent_confines_runtime_state_and_disables_timing(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    with audit_files.create_audit_run(project_root) as audit_run:
        audit_run.write_private_text("probe-input.txt", "marker\n")
        identity = audit_run.regular_file_identity("probe-input.txt")
        agent = audit._build_read_agent(ReadToolLLM(), audit_run, identity, _args())
        output_dir = audit_run.path

        assert agent.vision_preprocessor.cache_root == output_dir / "image-cache" / "tiles"
        assert Path(agent._sandbox.workdir) == output_dir
        assert agent._timing_log_enabled is False


@requires_posix_audit_files
def test_live_02_uses_real_react_with_one_real_read(tmp_path, monkeypatch):
    project_root = tmp_path / "project"
    project_root.mkdir()
    marker = "REAL-READ-MARKER"
    llm = ReadToolLLM()
    monkeypatch.setattr(audit, "_build_client", lambda entry, args: llm)
    monkeypatch.setenv("ASTRA_PROFILE_QUERY", "1")
    monkeypatch.setenv("TIMING_LOG_ALL", "1")
    runtime_root = Path(react_runtime.__file__).resolve().parents[2]
    normal_timing = runtime_root / ".astra" / "timing.log"
    normal_vision = runtime_root / ".astra" / "image-cache"
    timing_before = (
        normal_timing.read_bytes() if normal_timing.is_file() else None
    )
    vision_before = sorted(
        (str(path.relative_to(normal_vision)), path.stat().st_mtime_ns)
        for path in normal_vision.rglob("*")
    ) if normal_vision.is_dir() else []

    with audit_files.create_audit_run(project_root) as audit_run:
        audit_run.write_private_text("probe-input.txt", marker)
        identity = audit_run.regular_file_identity("probe-input.txt")
        output_dir = audit_run.path
        result = asyncio.run(
            audit._read_tool_probe(_entry(), _args(), audit_run, marker, identity)
        )

    assert result["status"] == "passed"
    assert result["tool_names"] == ["read_file"]
    assert result["read_call_count"] == 1
    assert result["marker_observed"] is True
    assert llm.calls == 1
    assert llm.exposed_tools == ["read_file"]
    timing_after = normal_timing.read_bytes() if normal_timing.is_file() else None
    vision_after = sorted(
        (str(path.relative_to(normal_vision)), path.stat().st_mtime_ns)
        for path in normal_vision.rglob("*")
    ) if normal_vision.is_dir() else []
    assert timing_after == timing_before
    assert vision_after == vision_before
    assert not (output_dir / ".astra" / "query-profile.jsonl").exists()


@requires_posix_audit_files
def test_live_02_path_swap_cannot_read_instructions_or_write_profile_outside(
    tmp_path, monkeypatch
):
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = "OUTSIDE-AGENTS-PRIVATE-SENTINEL"
    (outside / "AGENTS.md").write_text(sentinel, encoding="utf-8")
    marker = "DESCRIPTOR-MARKER"
    llm = ReadToolLLM()
    monkeypatch.setattr(audit, "_build_client", lambda entry, args: llm)
    monkeypatch.setenv("ASTRA_PROJECT_TRUST", "1")
    monkeypatch.setenv("ASTRA_PROFILE_QUERY", "1")
    real_build = audit._build_read_agent

    def build_then_swap(client, audit_run, marker_identity, args):
        agent = real_build(client, audit_run, marker_identity, args)
        held = audit_run.path.with_name(audit_run.path.name + "-held")
        audit_run.path.rename(held)
        audit_run.path.symlink_to(outside, target_is_directory=True)
        return agent

    monkeypatch.setattr(audit, "_build_read_agent", build_then_swap)

    with audit_files.create_audit_run(project_root) as audit_run:
        audit_run.write_private_text("probe-input.txt", marker)
        identity = audit_run.regular_file_identity("probe-input.txt")
        result = asyncio.run(
            audit._read_tool_probe(_entry(), _args(), audit_run, marker, identity)
        )

    captured = json.dumps(llm.message_batches, ensure_ascii=False)
    assert result["status"] == "passed"
    assert result["marker_observed"] is True
    assert sentinel not in captured
    assert sorted(path.name for path in outside.iterdir()) == ["AGENTS.md"]


@requires_posix_audit_files
def test_live_02_react_timeout_event_is_external_failure(tmp_path, monkeypatch):
    class TimeoutLLM(ReadToolLLM):
        async def chat_stream(self, messages, tools):
            del messages, tools
            raise LLMIdleTimeout("provider echoed api_key=do-not-persist")
            yield  # pragma: no cover

    project_root = tmp_path / "project"
    project_root.mkdir()
    marker = "TIMEOUT-MARKER"
    monkeypatch.setattr(audit, "_build_client", lambda entry, args: TimeoutLLM())

    with audit_files.create_audit_run(project_root) as audit_run:
        audit_run.write_private_text("probe-input.txt", marker)
        identity = audit_run.regular_file_identity("probe-input.txt")
        result = asyncio.run(
            audit._read_tool_probe(_entry(), _args(), audit_run, marker, identity)
        )

    assert result["status"] == "external_failure"
    assert result["result_status"] == "external_failure"
    assert result["read_call_count"] == 0


@requires_posix_audit_files
def test_live_02_multiple_successful_reads_are_product_failure(tmp_path, monkeypatch):
    class DuplicateReadAgent:
        async def reply_stream(self, message):
            del message
            for call_id in ("read-1", "read-2"):
                yield {
                    "type": "tool_calls",
                    "calls": [
                        {
                            "id": call_id,
                            "name": "read_file",
                            "arguments": "{}",
                        }
                    ],
                }
                yield {
                    "type": "tool_result",
                    "name": "read_file",
                    "output": "DUPLICATE-MARKER",
                    "error": "",
                }
            yield {"type": "done"}

    project_root = tmp_path / "project"
    project_root.mkdir()
    monkeypatch.setattr(audit, "_build_client", lambda entry, args: FakeClient())
    monkeypatch.setattr(
        audit,
        "_build_read_agent",
        lambda client, audit_run, marker_identity, args: DuplicateReadAgent(),
    )

    with audit_files.create_audit_run(project_root) as audit_run:
        audit_run.write_private_text("probe-input.txt", "DUPLICATE-MARKER")
        identity = audit_run.regular_file_identity("probe-input.txt")
        result = asyncio.run(
            audit._read_tool_probe(
                _entry(), _args(), audit_run, "DUPLICATE-MARKER", identity
            )
        )

    assert result["status"] == "product_failure"
    assert result["read_call_count"] == 2


class CancellationResistantClient:
    def __init__(self):
        self.calls = 0
        self.close_calls = 0
        self.first_cancel_suppressed = asyncio.Event()
        self.closed = asyncio.Event()

    async def chat_stream(self, messages, tools):
        del messages, tools
        self.calls += 1
        if self.calls != 1:
            raise AssertionError("health request must not run after missed cancellation bound")
        yield {"type": "chunk", "content": "READY"}
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.first_cancel_suppressed.set()
            await self.closed.wait()

    async def aclose(self):
        self.close_calls += 1
        self.closed.set()


def test_live_03_cancellation_resistance_is_bounded_and_fully_drained(monkeypatch):
    client = CancellationResistantClient()
    monkeypatch.setattr(audit, "CANCEL_FINISH_SECONDS", 0.01)
    monkeypatch.setattr(audit, "_build_client", lambda entry, args: client)

    async def scenario():
        result = await audit._cancellation_health_probe(_entry(), _args())
        current = asyncio.current_task()
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ]
        return result, pending

    result, pending = asyncio.run(scenario())

    assert result["status"] == "product_failure"
    assert result["cancellation_completed"] is False
    assert client.first_cancel_suppressed.is_set()
    assert client.close_calls == 1
    assert pending == []


def test_live_03_caller_cancellation_closes_and_drains_child(monkeypatch):
    client = CancellationResistantClient()
    monkeypatch.setattr(audit, "CANCEL_FINISH_SECONDS", 1.0)
    monkeypatch.setattr(audit, "_build_client", lambda entry, args: client)

    async def scenario():
        probe = asyncio.create_task(
            audit._cancellation_health_probe(_entry(), _args())
        )
        await asyncio.wait_for(client.first_cancel_suppressed.wait(), timeout=0.5)
        probe.cancel()
        with pytest.raises(asyncio.CancelledError):
            await probe
        current = asyncio.current_task()
        return [
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ]

    pending = asyncio.run(scenario())

    assert client.close_calls == 1
    assert pending == []


def test_probe_worker_hard_deadline_kills_stubborn_child(tmp_path):
    ready_path = tmp_path / "worker-ready"
    timeout = 10.0

    # Spawn imports this test module before entering the worker target. Keep
    # that startup inside the production deadline, with enough CI headroom to
    # reach the SIGTERM-resistant target whose cleanup this test exercises.
    result = asyncio.run(
        asyncio.wait_for(
            audit._run_probe_worker(
                "LIVE-03",
                {"ready_path": str(ready_path)},
                timeout,
                worker_target=_stubborn_worker,
            ),
            timeout=20.0,
        )
    )

    assert ready_path.is_file(), result
    ready = json.loads(ready_path.read_text(encoding="utf-8"))
    assert ready["ready"] is True and ready["pid"] != os.getpid()
    assert result["status"] == "external_failure"
    assert result["error_type"] == "ProbeTimeout"
    # The report truncates floating elapsed milliseconds; allow only that 1 ms.
    assert result["duration_ms"] >= timeout * 1000 - 1
    assert not pid_alive(ready["pid"])
    assert multiprocessing.active_children() == []


@pytest.mark.parametrize(
    ("worker_target", "error_type"),
    [
        (_oversize_wire_worker, "ProbeIPCFailure"),
        (_non_json_worker, "InvalidProbeResult"),
    ],
)
@pytest.mark.usefixtures("ready_audit_worker")
def test_worker_wire_never_unpickles_or_accepts_unbounded_non_json(
    worker_target, error_type
):
    result = asyncio.run(
        audit_worker.run_probe_worker(
            "LIVE-01", {}, 1.0, worker_target=worker_target
        )
    )

    assert result["status"] == "product_failure"
    assert result["error_type"] == error_type
    assert multiprocessing.active_children() == []


def test_worker_result_cap_fits_conservative_pipe_payload():
    assert audit_worker.MAX_WORKER_RESULT_BYTES == 2_048
    assert audit_worker.MAX_WORKER_RESULT_BYTES + 4 <= 4_096


@pytest.mark.parametrize(
    "result",
    [
        {
            "id": "LIVE-01",
            "status": "passed",
            "duration_ms": 180_000,
            "event_types": ["reasoning", "chunk", "done"],
            "finish_reason": "stop",
            "usage_totals": {
                "prompt_tokens": 2_147_483_647,
                "completion_tokens": 2_147_483_647,
                "total_tokens": 2_147_483_647,
            },
            "final_text_length": 2_147_483_647,
        },
        {
            "id": "LIVE-02",
            "status": "passed",
            "duration_ms": 180_000,
            "tool_names": ["read_file"],
            "result_status": "succeeded",
            "read_call_count": 1,
            "marker_observed": True,
        },
        {
            "id": "LIVE-03",
            "status": "passed",
            "duration_ms": 180_000,
            "delta_observed": True,
            "stream_was_active": True,
            "cancellation_completed": True,
            "cancel_wait_ms": 5_000,
            "health_request_succeeded": True,
        },
    ],
)
def test_legitimate_worker_results_fit_wire_cap(result):
    class CaptureConnection:
        payload = b""

        def send_bytes(self, payload):
            self.payload = payload

    connection = CaptureConnection()

    audit_worker.send_worker_result(connection, result)

    assert json.loads(connection.payload) == result
    assert len(connection.payload) <= audit_worker.MAX_WORKER_RESULT_BYTES


@pytest.mark.usefixtures("ready_audit_worker")
def test_worker_wire_does_not_execute_malicious_pickle_in_parent(tmp_path):
    side_effect = tmp_path / "unpickled-in-parent"

    result = asyncio.run(
        audit_worker.run_probe_worker(
            "LIVE-01",
            {"side_effect_path": str(side_effect)},
            1.0,
            worker_target=_evil_pickle_worker,
        )
    )

    assert result["error_type"] == "InvalidProbeResult"
    assert not side_effect.exists()
    assert multiprocessing.active_children() == []


@pytest.mark.skipif(os.name != "posix", reason="partial byte framing uses a POSIX pipe descriptor")
def test_partial_worker_frame_cannot_block_past_deadline_and_is_hard_killed(tmp_path, monkeypatch):
    ready = tmp_path / "ready"
    side_effect = tmp_path / "side-effect"
    operation_started = []
    make_process = audit_worker._make_process

    class ReadyProcess:
        def __init__(self, process):
            self.process = process

        def __getattr__(self, name):
            return getattr(self.process, name)

        def start(self):
            self.process.start()
            # Establish the partial-frame precondition before measuring its
            # deadline. Cold spawn imports are independently tested elsewhere.
            startup_deadline = time.monotonic() + 10
            while not ready.exists() or not side_effect.exists():
                if not self.process.is_alive() or time.monotonic() >= startup_deadline:
                    raise RuntimeError("partial-frame worker did not become ready")
                time.sleep(0.01)
            operation_started.append(time.monotonic())

    monkeypatch.setattr(
        audit_worker, "_make_process",
        lambda *args, **kwargs: ReadyProcess(make_process(*args, **kwargs)),
    )

    result = asyncio.run(
        audit_worker.run_probe_worker(
            "LIVE-01",
            {"ready_path": str(ready), "side_effect_path": str(side_effect), "startup_delay": 0.6},
            0.5,
            worker_target=_partial_wire_worker,
        )
    )

    elapsed = time.monotonic() - operation_started[0]
    assert ready.read_text(encoding="utf-8") == "ready"
    assert result["error_type"] == "ProbeTimeout"
    assert elapsed < 1.5
    frozen = side_effect.read_text(encoding="utf-8")
    time.sleep(0.1)
    assert side_effect.read_text(encoding="utf-8") == frozen
    assert multiprocessing.active_children() == []


@requires_posix_audit_files
def test_audit_run_rejects_preexisting_hardlink_without_outside_mutation(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-original", encoding="utf-8")
    before_mode = outside.stat().st_mode

    with audit_files.create_audit_run(project_root) as audit_run:
        os.link(outside, audit_run.path / "probe-input.txt")
        with pytest.raises(RuntimeError, match="hardlink|multi-link"):
            audit_run.write_private_text("probe-input.txt", "replacement")

    assert outside.read_text(encoding="utf-8") == "outside-original"
    assert outside.stat().st_mode == before_mode


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO is POSIX-only")
@requires_posix_audit_files
def test_audit_run_rejects_fifo_without_blocking(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    with audit_files.create_audit_run(project_root) as audit_run:
        fifo = audit_run.path / "probe-input.txt"
        os.mkfifo(fifo)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="special|regular"):
            audit_run.write_private_text("probe-input.txt", "replacement")
        assert time.monotonic() - started < 0.5
        assert fifo.is_fifo()


@requires_posix_audit_files
def test_descriptor_relative_write_survives_intermediate_path_swap(
    tmp_path, monkeypatch
):
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with audit_files.create_audit_run(project_root) as audit_run:
        audit_run.prepare_runtime()
        held = audit_run.path / "held-image-cache"
        real_open = audit_files.os.open
        swapped = False

        def racing_open(path, flags, mode=0o777, *, dir_fd=None):
            nonlocal swapped
            descriptor = real_open(path, flags, mode, dir_fd=dir_fd)
            if path == "image-cache" and dir_fd is not None and not swapped:
                swapped = True
                (audit_run.path / "image-cache").rename(held)
                (audit_run.path / "image-cache").symlink_to(
                    outside, target_is_directory=True
                )
            return descriptor

        monkeypatch.setattr(audit_files.os, "open", racing_open)
        audit_run.write_private_text("image-cache/race.txt", "descriptor-owned")

        assert (held / "race.txt").read_text(encoding="utf-8") == "descriptor-owned"
        assert list(outside.iterdir()) == []


@requires_posix_audit_files
def test_audit_read_tool_survives_run_path_replacement_without_reading_outside(
    tmp_path
):
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    with audit_files.create_audit_run(project_root) as audit_run:
        audit_run.write_private_text("probe-input.txt", "descriptor-marker\n")
        identity = audit_run.regular_file_identity("probe-input.txt")
        registry = audit._build_audit_read_registry(audit_run, identity)
        held = audit_run.path.with_name(audit_run.path.name + "-held")
        audit_run.path.rename(held)
        audit_run.path.symlink_to(outside, target_is_directory=True)
        (outside / "probe-input.txt").write_text("outside-secret", encoding="utf-8")

        result = asyncio.run(registry.execute("read_file", {"path": "probe-input.txt"}))

        assert result["error"] == ""
        assert result["output"] == "descriptor-marker\n"
        assert "outside-secret" not in result["output"]


@requires_posix_audit_files
def test_audit_read_tool_rejects_replaced_target_inode(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    with audit_files.create_audit_run(project_root) as audit_run:
        audit_run.write_private_text("probe-input.txt", "expected-marker\n")
        identity = audit_run.regular_file_identity("probe-input.txt")
        registry = audit._build_audit_read_registry(audit_run, identity)
        (audit_run.path / "probe-input.txt").unlink()
        (audit_run.path / "probe-input.txt").write_text(
            "replacement-secret", encoding="utf-8"
        )

        result = asyncio.run(registry.execute("read_file", {"path": "probe-input.txt"}))

        assert result["error"]
        assert "replacement-secret" not in result["output"]


@requires_posix_audit_files
def test_audit_read_tool_rejects_same_inode_same_size_content_swap(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()
    original = "expected-marker\n"
    replacement = "S" * len(original)

    with audit_files.create_audit_run(project_root) as audit_run:
        audit_run.write_private_text("probe-input.txt", original)
        identity = audit_run.regular_file_identity("probe-input.txt")
        registry = audit._build_audit_read_registry(audit_run, identity)
        target = audit_run.path / "probe-input.txt"
        before = target.stat()
        with target.open("r+b") as handle:
            handle.write(replacement.encode("utf-8"))
            handle.flush()
        after = target.stat()
        assert (after.st_dev, after.st_ino, after.st_size) == (
            before.st_dev,
            before.st_ino,
            before.st_size,
        )

        result = asyncio.run(registry.execute("read_file", {"path": "probe-input.txt"}))

        assert result["error"]
        assert replacement not in result["output"]


@requires_posix_audit_files
def test_marker_identity_capture_rejects_content_changed_after_exclusive_write(
    tmp_path,
):
    project_root = tmp_path / "project"
    project_root.mkdir()
    expected = b"expected-marker\n"

    with audit_files.create_audit_run(project_root) as audit_run:
        audit_run.write_private_text("probe-input.txt", expected.decode("utf-8"))
        target = audit_run.path / "probe-input.txt"
        target.write_bytes(b"S" * len(expected))

        with pytest.raises(RuntimeError, match="content changed"):
            audit_run.regular_file_identity(
                "probe-input.txt", expected_content=expected
            )


@pytest.mark.parametrize("path", ["../probe-input.txt", "/tmp/probe-input.txt", "other.txt"])
@requires_posix_audit_files
def test_audit_read_tool_accepts_only_exact_probe_filename(tmp_path, path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    with audit_files.create_audit_run(project_root) as audit_run:
        audit_run.write_private_text("probe-input.txt", "marker\n")
        identity = audit_run.regular_file_identity("probe-input.txt")
        registry = audit._build_audit_read_registry(audit_run, identity)

        result = asyncio.run(registry.execute("read_file", {"path": path}))

        assert result["error"]
        assert result["output"] == ""


@pytest.mark.parametrize("artifact_kind", ["hardlink", "fifo"])
@requires_posix_audit_files
def test_runtime_preflight_rejects_nested_unsafe_artifacts(tmp_path, artifact_kind):
    if artifact_kind == "fifo" and not hasattr(os, "mkfifo"):
        pytest.skip("FIFO is POSIX-only")
    project_root = tmp_path / "project"
    project_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside-original", encoding="utf-8")

    with audit_files.create_audit_run(project_root) as audit_run:
        artifact = audit_run.path / "tool-results" / "unsafe"
        if artifact_kind == "hardlink":
            os.link(outside, artifact)
        else:
            os.mkfifo(artifact)

        with pytest.raises(RuntimeError, match="multi-link|special"):
            audit_run.prepare_runtime()

    assert outside.read_text(encoding="utf-8") == "outside-original"


@pytest.mark.parametrize(
    ("worker_target", "error_type"),
    [
        (_malformed_worker, "InvalidProbeResult"),
        (_unexpected_exit_worker, "ProbeWorkerExit"),
    ],
)
@pytest.mark.usefixtures("ready_audit_worker")
def test_worker_runtime_failures_are_product_failures(worker_target, error_type):
    result = asyncio.run(
        audit._run_probe_worker(
            "LIVE-01", {}, 1.0, worker_target=worker_target
        )
    )

    assert result["status"] == "product_failure"
    assert result["error_type"] == error_type


@pytest.mark.usefixtures("ready_audit_worker")
def test_hostile_worker_result_cannot_persist_secret_fields_or_error_types():
    result = asyncio.run(
        audit._run_probe_worker(
            "LIVE-01", {}, 1.0, worker_target=_hostile_worker
        )
    )
    persisted = json.dumps(result)

    assert result["status"] == "product_failure"
    assert result["error_type"] == "OtherError"
    assert "secret_field" not in result
    assert "ipc-secret" not in persisted


@pytest.mark.parametrize("probe_id", ["LIVE-01", "LIVE-02", "LIVE-03"])
@pytest.mark.usefixtures("ready_audit_worker")
def test_parent_rejects_hostile_pass_without_required_probe_evidence(probe_id):
    result = asyncio.run(
        audit._run_probe_worker(
            probe_id,
            {"result": {"status": "passed"}},
            1.0,
            worker_target=_payload_worker,
        )
    )

    assert result["status"] == "product_failure"


@requires_posix_audit_files
def test_two_hostile_pass_results_cannot_make_overall_audit_pass(
    tmp_path, monkeypatch
):
    _install_fakes(monkeypatch, tmp_path)
    calls = []
    hostile = {
        "LIVE-01": {"status": "passed"},
        "LIVE-02": {"status": "passed"},
        "LIVE-03": {
            "status": "passed",
            "delta_observed": True,
            "stream_was_active": True,
            "cancellation_completed": True,
            "cancel_wait_ms": 1,
            "health_request_succeeded": True,
        },
    }

    async def hostile_worker(probe_id, payload, timeout, **kwargs):
        del payload, timeout, kwargs
        calls.append(probe_id)
        return audit_results.normalize_worker_result(probe_id, hostile[probe_id])

    monkeypatch.setattr(audit, "_run_probe_worker", hostile_worker)

    report = asyncio.run(run_audit(_args()))

    assert calls == ["LIVE-01", "LIVE-02", "LIVE-03"]
    assert [probe["status"] for probe in report["probes"]] == [
        "product_failure",
        "product_failure",
        "passed",
    ]
    assert report["passed"] is False


@requires_posix_audit_files
def test_malformed_pickle_safe_shapes_are_total_and_later_probes_continue(
    tmp_path, monkeypatch
):
    _install_fakes(monkeypatch, tmp_path)
    calls = []
    huge = 10**10_000
    malformed = {
        "LIVE-01": {
            "status": [],
            "duration_ms": float("inf"),
            "event_types": [[], {"api_key": "ipc-secret"}],
            "finish_reason": {"api_key": "ipc-secret"},
            "usage_totals": {"total_tokens": huge},
            "final_text_length": huge,
        },
        "LIVE-02": {
            "status": {"bad": "shape"},
            "tool_names": [[], {"token": "ipc-secret"}],
            "result_status": [],
            "read_call_count": float("inf"),
            "marker_observed": {"bad": "shape"},
        },
        "LIVE-03": {
            "status": ["passed"],
            "delta_observed": [],
            "stream_was_active": {},
            "cancellation_completed": {"bad": "shape"},
            "cancel_wait_ms": huge,
            "health_request_succeeded": [True],
        },
    }

    async def malformed_worker(probe_id, payload, timeout, **kwargs):
        del payload, timeout, kwargs
        calls.append(probe_id)
        return audit_results.normalize_worker_result(probe_id, malformed[probe_id])

    monkeypatch.setattr(audit, "_run_probe_worker", malformed_worker)

    report = asyncio.run(run_audit(_args()))
    persisted = Path(report["report_path"]).read_text(encoding="utf-8")

    assert calls == ["LIVE-01", "LIVE-02", "LIVE-03"]
    assert report["passed"] is False
    assert all(probe["status"] == "product_failure" for probe in report["probes"])
    assert "ipc-secret" not in persisted


_MISSING = object()


@pytest.mark.parametrize(
    ("probe_id", "numeric_field", "base_result"),
    [
        (
            "LIVE-01",
            "final_text_length",
            {"status": "passed", "event_types": ["done"]},
        ),
        (
            "LIVE-02",
            "read_call_count",
            {
                "status": "passed",
                "tool_names": ["read_file"],
                "result_status": "succeeded",
                "marker_observed": True,
            },
        ),
        (
            "LIVE-03",
            "cancel_wait_ms",
            {
                "status": "passed",
                "delta_observed": True,
                "stream_was_active": True,
                "cancellation_completed": True,
                "health_request_succeeded": True,
            },
        ),
    ],
)
def test_required_numeric_evidence_rejects_hostile_shapes(
    probe_id, numeric_field, base_result
):
    hostile_values = [
        _MISSING,
        True,
        -1,
        1.0,
        float("inf"),
        "1",
        10**10_000,
    ]

    for value in hostile_values:
        result = dict(base_result)
        if value is not _MISSING:
            result[numeric_field] = value

        normalized = audit_results.normalize_worker_result(probe_id, result)

        assert normalized["status"] == "product_failure", (probe_id, value)


@requires_posix_audit_files
def test_two_malformed_numeric_passes_cannot_make_overall_audit_pass(
    tmp_path, monkeypatch
):
    _install_fakes(monkeypatch, tmp_path)
    calls = []
    hostile = {
        "LIVE-01": {
            "status": "passed",
            "event_types": ["done"],
            "final_text_length": True,
        },
        "LIVE-02": {
            "status": "passed",
            "tool_names": ["read_file"],
            "result_status": "succeeded",
            "read_call_count": "1",
            "marker_observed": True,
        },
        "LIVE-03": {
            "status": "passed",
            "delta_observed": True,
            "stream_was_active": True,
            "cancellation_completed": True,
            "cancel_wait_ms": 1,
            "health_request_succeeded": True,
        },
    }

    async def hostile_worker(probe_id, payload, timeout, **kwargs):
        del payload, timeout, kwargs
        calls.append(probe_id)
        return audit_results.normalize_worker_result(probe_id, hostile[probe_id])

    monkeypatch.setattr(audit, "_run_probe_worker", hostile_worker)

    report = asyncio.run(run_audit(_args()))

    assert calls == ["LIVE-01", "LIVE-02", "LIVE-03"]
    assert [probe["status"] for probe in report["probes"]] == [
        "product_failure",
        "product_failure",
        "passed",
    ]
    assert report["passed"] is False


class NeverReapedProcess:
    pid = 12345

    def __init__(self):
        self.terminate_calls = 0
        self.kill_calls = 0
        self.join_calls = []

    def is_alive(self):
        return True

    def terminate(self):
        self.terminate_calls += 1

    def kill(self):
        self.kill_calls += 1

    def join(self, timeout=None):
        self.join_calls.append(timeout)


class SlowStartingProcess:
    pid = None

    def __init__(self):
        self.alive = False

    def start(self):
        time.sleep(0.05)
        self.pid = 24680
        self.alive = False

    def is_alive(self):
        return self.alive

    def terminate(self):
        self.alive = False

    def kill(self):
        self.alive = False

    def join(self, timeout=None):
        del timeout


def test_worker_operation_deadline_starts_after_successful_process_start(monkeypatch):
    process = SlowStartingProcess()
    monkeypatch.setattr(audit_worker, "_make_process", lambda *args, **kwargs: process)
    # Advance only the worker's clock: Windows monotonic ticks can be 15.6 ms.
    # A deadline incorrectly set before start() must still expire here.
    from types import SimpleNamespace

    now = [0.0]
    def start():
        now[0] += 0.05
        process.pid = 24680
    monkeypatch.setattr(process, "start", start)
    monkeypatch.setattr(audit_worker, "time", SimpleNamespace(monotonic=lambda: now[0]))

    result = asyncio.run(
        audit_worker.run_probe_worker(
            "LIVE-01",
            {},
            0.01,
            worker_target=_unexpected_exit_worker,
        )
    )

    assert result["status"] == "product_failure"
    assert result["error_type"] != "ProbeTimeout"
    assert now[0] == 0.05
    assert process.is_alive() is False


def test_worker_kills_at_deadline_despite_truncated_duration(monkeypatch):
    process = SlowStartingProcess()
    now = [1014.004]
    timeout = 10.0
    deadline = now[0] + timeout
    sleeps, killed_at, joins = [], [], []

    def start():
        process.pid = 24680
        process.alive = True

    def kill():
        killed_at.append(now[0])
        process.alive = False

    async def advance_clock(delay):
        assert now[0] < deadline
        assert process.is_alive() and not killed_at
        assert 0 < delay <= audit_worker._POLL_SECONDS
        sleeps.append(now[0])
        now[0] = math.nextafter(deadline, -math.inf) if len(sleeps) == 1 else deadline

    monkeypatch.setattr(process, "start", start)
    monkeypatch.setattr(process, "kill", kill)
    monkeypatch.setattr(process, "join", lambda timeout=None: joins.append(timeout))
    monkeypatch.setattr(audit_worker, "_make_process", lambda *args, **kwargs: process)
    monkeypatch.setattr(audit_worker, "time", SimpleNamespace(monotonic=lambda: now[0]))
    monkeypatch.setattr(
        audit_worker, "asyncio",
        SimpleNamespace(sleep=advance_clock, CancelledError=asyncio.CancelledError),
    )

    result = asyncio.run(
        audit_worker.run_probe_worker(
            "LIVE-03", {}, timeout, worker_target=_stubborn_worker,
        )
    )

    assert sleeps == [1014.004, math.nextafter(deadline, -math.inf)]
    assert killed_at == [deadline]
    assert joins == [None]
    assert process.is_alive() is False
    assert result["status"] == "external_failure"
    assert result["error_type"] == "ProbeTimeout"
    # Exact deadline subtraction can be 9999.999999999887 ms before int().
    assert result["duration_ms"] >= timeout * 1000 - 1


def test_timeout_uses_immediate_hard_kill_without_sigterm_grace(monkeypatch):
    process = NeverReapedProcess()
    monkeypatch.setattr(audit_worker, "_make_process", lambda *args, **kwargs: process)
    process.start = lambda: None

    with pytest.raises(audit_worker.ProbeManagementError):
        asyncio.run(
            audit_worker.run_probe_worker(
                "LIVE-01", {}, 0.001, worker_target=_unexpected_exit_worker
            )
        )

    assert process.terminate_calls == 0
    assert process.kill_calls == 1


def test_unreapable_worker_raises_management_failure():
    process = NeverReapedProcess()

    with pytest.raises(audit_worker.ProbeManagementError):
        audit_worker._stop_process(process)

    assert process.terminate_calls == 1
    assert process.kill_calls == 1
    assert len(process.join_calls) >= 2


@requires_posix_audit_files
def test_unresolved_model_key_never_persists_raw_unlabeled_secret(
    tmp_path, monkeypatch
):
    raw_secret = "sk-live-secret"
    args = _args()
    args.model_key = raw_secret

    async def fail_resolution(_args):
        raise LookupError("not found")

    monkeypatch.setattr(audit, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(audit, "_resolve_entry", fail_resolution)
    monkeypatch.setattr(audit, "load_project_env", lambda project_root: None)

    report = asyncio.run(run_audit(args))
    persisted = Path(report["report_path"]).read_text(encoding="utf-8")

    assert report["model_key"] == "unresolved"
    assert raw_secret not in persisted
