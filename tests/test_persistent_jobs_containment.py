"""Scheduled-worker cleanup owns the interpreter, not just its launcher PID."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.cli import jobs_worker
from agent.runtime.instance_lock import InstanceAlreadyRunning, InstanceLock
from agent.runtime.jobs import scheduler
from agent.runtime.jobs.store import JobStore


def occurrence(tmp_path):
    store = JobStore(tmp_path / "home")
    job = store.add(name="check", prompt="fixture", original_request="fixture", model="fixture",
                    workdir=tmp_path, next_at=1000, interval_seconds=60, idle_seconds=1, timeout_seconds=5)
    return store, store.claim_due(now=1000), job


def kill_fixture(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        if not isinstance(exc, ProcessLookupError) and getattr(exc, "winerror", None) != 87:
            raise  # Windows reports an already exited PID as INVALID_PARAMETER.


@pytest.mark.parametrize("wrapper_exits", [False, True])
def test_wrapper_cleanup_releases_owner_and_child_locks_without_stopping_other_home(tmp_path, monkeypatch, wrapper_exits):
    store, run, _ = occurrence(tmp_path)
    ready = tmp_path / "ready.json"
    child_lock = tmp_path / "child.lock"
    child_script = "from agent.runtime.instance_lock import InstanceLock; import os,sys,time; lock=InstanceLock(sys.argv[1]); lock.acquire(); print(os.getpid(),flush=True); time.sleep(60)"
    owner_script = """
import json,os,subprocess,sys,time
from pathlib import Path
from agent.runtime.jobs.store import JobStore
store=JobStore(Path(sys.argv[1]))
with store.run_lock(sys.argv[2]):
 store.start(sys.argv[2])
 child=subprocess.Popen([sys.executable,'-c',sys.argv[5],sys.argv[4]],stdout=subprocess.PIPE,text=True)
 child_pid=int(child.stdout.readline().strip())
 Path(sys.argv[3]).write_text(json.dumps([os.getpid(),child_pid]))
 time.sleep(60)
"""
    wrapper_script = """
import os,subprocess,sys,time
from pathlib import Path
if os.name=='nt' and sys.stdin.buffer.read(1)!=b'1': sys.exit(1)
child=subprocess.Popen(sys.argv[3:])
while not Path(sys.argv[1]).exists(): time.sleep(.01)
if sys.argv[2]=='exit': sys.exit(0)
sys.exit(child.wait())
"""
    original_popen = subprocess.Popen
    wrappers = []
    def spawn(_args, **kwargs):
        executable = _args[0]  # preserves the Windows base-interpreter launch
        child = [executable, "-c", owner_script, str(store.home), run["id"], str(ready), str(child_lock), child_script]
        wrapper = original_popen([executable, "-c", wrapper_script, str(ready),
                                  "exit" if wrapper_exits else "wait", *child], **kwargs)
        wrappers.append(wrapper)
        return wrapper

    # A separate home has a live owner in another process, outside this group/job.
    other_store, other_run, _ = occurrence(tmp_path / "other")
    unrelated_script = "from pathlib import Path; import os,sys,time; from agent.runtime.jobs.store import JobStore; s=JobStore(Path(sys.argv[1])); l=s.run_lock(sys.argv[2]); l.acquire(); s.start(sys.argv[2]); print(os.getpid(),flush=True); time.sleep(60)"
    unrelated = original_popen([sys.executable, "-c", unrelated_script, str(other_store.home), other_run["id"]],
                              stdout=subprocess.PIPE, text=True)
    unrelated_pid = None
    cleanup_confirmed = False
    try:
        unrelated_pid = int(unrelated.stdout.readline().strip())
        monkeypatch.setattr(scheduler.subprocess, "Popen", spawn)
        scheduler.execute(store, run)
        assert wrappers[0].poll() is not None
        assert store.run(run["id"])["state"] == "unknown"
        with store.run_lock(run["id"]), InstanceLock(child_lock):
            pass  # both actual processes released their kernel-owned handles
        cleanup_confirmed = True
        assert unrelated.poll() is None
        with pytest.raises(InstanceAlreadyRunning):
            other_store.run_lock(other_run["id"]).acquire()
    finally:
        if not cleanup_confirmed and ready.exists():
            for pid in json.loads(ready.read_text()):
                kill_fixture(pid)
        for wrapper in wrappers:
            if wrapper.poll() is None:
                wrapper.kill()
            wrapper.wait(timeout=5)
        if unrelated_pid is not None:
            kill_fixture(unrelated_pid)
        else:
            unrelated.terminate()
        unrelated.wait(timeout=5)
        unrelated.stdout.close()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX provider cancellation and detached process groups")
def test_wrapper_exit_does_not_cut_off_detached_provider_finally(tmp_path, monkeypatch):
    store, run, _ = occurrence(tmp_path)
    child_lock, cleaned = tmp_path / "provider.lock", tmp_path / "cleaned"
    ready, diagnostics = tmp_path / "provider-ready", tmp_path / "worker-stderr.log"
    script = """
import asyncio,subprocess,sys
from pathlib import Path
from agent.cli import jobs_worker
from agent.runtime.claude_code_provider import _terminate
from agent.runtime.jobs.store import JobStore
store=JobStore(Path(sys.argv[1]))
async def check(*_):
 child=await asyncio.create_subprocess_exec(sys.executable,'-c',
  "from agent.runtime.instance_lock import InstanceLock; import sys,time; l=InstanceLock(sys.argv[1]); l.acquire(); print('ready',flush=True); time.sleep(60)",
  sys.argv[3],stdout=asyncio.subprocess.PIPE,start_new_session=True)
 try:
  assert (await child.stdout.readline()).strip()==b'ready'
  Path(sys.argv[5]).write_text('provider cancellation is ready')
  await asyncio.sleep(60)
 finally:
  await asyncio.sleep(.3)
  _terminate(child)
  await child.wait()
  Path(sys.argv[4]).write_text('provider finally completed')
jobs_worker.run_check=check
with store.run_lock(sys.argv[2]):
 store.start(sys.argv[2])
 asyncio.run(jobs_worker.run_with_shutdown(store,store.run(sys.argv[2])))
"""
    original_popen = subprocess.Popen
    wrappers = []
    def spawn(_args, **kwargs):
        child = [sys.executable, "-c", script, str(store.home), run["id"], str(child_lock), str(cleaned), str(ready)]
        with diagnostics.open("wb") as stderr:
            process = original_popen([sys.executable, "-c", "import subprocess,sys; sys.exit(subprocess.call(sys.argv[1:]))", *child],
                                     **{**kwargs, "stderr": stderr})
        wrappers.append(process)
        deadline = time.monotonic() + 10
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        if not ready.exists():
            # Startup is outside the behavior under test. Keep it bounded and
            # clean up before reporting useful fixture diagnostics.
            scheduler._stop_worker(process, store=store, identity=run["id"])
            raise AssertionError(f"Provider fixture did not become ready (exit {process.returncode}): "
                                 + diagnostics.read_text(errors="replace"))
        # Start the production idle budget only once cancellation can exercise
        # the provider's finally block, regardless of CI import/startup speed.
        return process
    monkeypatch.setattr(scheduler.subprocess, "Popen", spawn)
    try:
        scheduler.execute(store, run)
        assert cleaned.read_text() == "provider finally completed"
        with store.run_lock(run["id"]), InstanceLock(child_lock):
            pass
        assert store.run(run["id"])["state"] == "unknown"
        assert wrappers[0].poll() is not None
    finally:
        # If this assertion exposes a regression, do not leave the deliberately
        # detached fixture process behind. Its lock still names its own PID.
        if not cleaned.exists() and child_lock.exists():
            pid = int(child_lock.read_text().strip())
            if pid != os.getpid():
                kill_fixture(pid)


def test_unconfirmed_cleanup_keeps_active_occurrence_fenced(tmp_path, monkeypatch):
    store, run, job = occurrence(tmp_path)
    owner = store.run_lock(run["id"])
    owner.acquire()
    store.start(run["id"])
    monkeypatch.setattr(scheduler, "_spawn_worker", lambda *_: (SimpleNamespace(poll=lambda: 0), None))
    monkeypatch.setattr(scheduler, "_stop_worker", lambda *_, **__: None)
    wait = scheduler._wait_for_owner_exit
    monkeypatch.setattr(scheduler, "_wait_for_owner_exit", lambda store, identity: wait(store, identity, deadline=time.monotonic()))
    try:
        with pytest.raises(OSError, match="still running"):
            scheduler.execute(store, run)
        assert store.run(run["id"])["state"] == "running"
        assert scheduler.recover_abandoned(store) == 0
        assert store.claim_due(now=2000) is None
        assert len(store.history(job["id"])) == 1
    finally:
        owner.release()
    assert scheduler.recover_abandoned(store) == 1
    assert store.run(run["id"])["state"] == "unknown"


@pytest.mark.parametrize("owner_started", [False, True])
def test_startup_cleanup_failure_preserves_fence_before_and_after_owner_lease(tmp_path, monkeypatch, owner_started):
    store, run, job = occurrence(tmp_path)
    owner = store.run_lock(run["id"])
    if owner_started:
        owner.acquire()
        store.start(run["id"])
    class Pipe:
        def write(self, _):
            pass  # The token reached the child; it can now acquire its lease.
        def close(self):
            raise OSError("gate release could not be confirmed")
    process = SimpleNamespace(_handle=77, stdin=Pipe())
    monkeypatch.setattr(scheduler.sys, "platform", "win32")
    monkeypatch.setattr(scheduler, "hidden_process_creationflags", lambda **_: 0)
    monkeypatch.setattr(scheduler.subprocess, "Popen", lambda *_, **__: process)
    monkeypatch.setattr(scheduler.WindowsJob, "create", lambda **_: SimpleNamespace(close=lambda: None))
    def failed_cleanup(*_, **__):
        raise OSError("owned process termination could not be confirmed")
    monkeypatch.setattr(scheduler, "_stop_worker", failed_cleanup)
    try:
        with pytest.raises(scheduler.WorkerCleanupUnconfirmed, match="cleanup could not be confirmed"):
            scheduler.execute(store, run)
        assert store.run(run["id"])["state"] == ("running" if owner_started else "claimed")
        assert store.claim_due(now=2000) is None
        assert len(store.history(job["id"])) == 1
        if owner_started:
            assert scheduler.recover_abandoned(store) == 0
    finally:
        owner.release()
    assert scheduler.recover_abandoned(store) == 1
    # A late worker can take its lease but cannot start the terminal occurrence.
    with store.run_lock(run["id"]):
        assert not store.start(run["id"])
    assert store.run(run["id"])["state"] == "unknown"


def test_safely_stopped_startup_keeps_admitted_occurrence_unknown(tmp_path, monkeypatch):
    store, run, _ = occurrence(tmp_path)
    def admitted_then_stopped(*_):
        with store.run_lock(run["id"]):
            store.start(run["id"])
        raise OSError("gate close failed after worker started and was stopped")
    monkeypatch.setattr(scheduler, "_spawn_worker", admitted_then_stopped)
    scheduler.execute(store, run)
    assert store.run(run["id"])["state"] == "unknown"


@pytest.mark.parametrize("failure", ["", "assignment", "release"])
def test_windows_job_is_assigned_before_gate_and_failed_startup_is_cleaned(tmp_path, monkeypatch, failure):
    store, run, _ = occurrence(tmp_path)
    events, launches = [], []
    class Pipe:
        def write(self, data):
            events.append(("release", data))
            if failure == "release":
                raise BrokenPipeError("fixture")
        def close(self):
            events.append(("pipe_closed",))
    class Process:
        _handle = 77
        stdin = Pipe()
        returncode = None
        def poll(self):
            return self.returncode
        def kill(self):
            events.append(("killed",))
            self.returncode = -1
        def wait(self, timeout):
            events.append(("waited",))
            return self.returncode
    process = Process()
    def create_job(**kwargs):
        events.append(("assigned", kwargs["process_handle"]))
        if failure == "assignment":
            raise OSError("fixture")
        return SimpleNamespace(close=lambda: events.append(("job_closed",)))
    monkeypatch.setattr(scheduler.sys, "platform", "win32")
    monkeypatch.setattr(scheduler.sys, "executable", "/fixture/venv/python.exe")
    monkeypatch.setattr(scheduler.sys, "_base_executable", "/fixture/base/python.exe")
    monkeypatch.setattr(scheduler, "hidden_process_creationflags", lambda **kwargs: 512)
    monkeypatch.setattr(scheduler.WindowsJob, "create", create_job)
    monkeypatch.setattr(scheduler.subprocess, "Popen", lambda args, **kwargs: launches.append((args, kwargs)) or process)
    if failure:
        scheduler.execute(store, run)
        assert store.run(run["id"])["state"] == "failed"
        assert ("killed",) in events and ("waited",) in events
        if failure == "release":
            assert ("job_closed",) in events
    else:
        spawned, job = scheduler._spawn_worker(store, run)
        assert events[:2] == [("assigned", 77), ("release", b"1")]
        process.returncode = 0  # root exits before its owned job is closed
        scheduler._stop_worker(spawned, job)
        assert ("job_closed",) in events
    args, kwargs = launches[0]
    assert args[0] == "/fixture/base/python.exe"
    assert kwargs["env"]["__PYVENV_LAUNCHER__"] == "/fixture/venv/python.exe"
    assert kwargs["stdin"] == subprocess.PIPE and kwargs["start_new_session"] is False


def test_worker_start_gate_exits_on_parent_eof_without_running_check():
    result = subprocess.run([sys.executable, "-c", scheduler._WINDOWS_WORKER_GATE], input=b"", capture_output=True, timeout=5)
    assert result.returncode == 1
    assert result.stderr == b""


@pytest.mark.skipif(sys.platform != "win32", reason="Requires native Windows venv launch and Job Object")
def test_windows_base_launch_keeps_venv_site_packages(tmp_path, monkeypatch):
    store, run, _ = occurrence(tmp_path)
    target = tmp_path / "interpreter.json"
    monkeypatch.setenv("ASTRA_TEST_INTERPRETER_RESULT", str(target))
    gate = "import sys; token=sys.stdin.buffer.read(1); sys.exit(1) if token!=b'1' else None; import json,os,pytest; from pathlib import Path; Path(os.environ['ASTRA_TEST_INTERPRETER_RESULT']).write_text(json.dumps({'prefix':sys.prefix,'pytest':pytest.__file__}))"
    monkeypatch.setattr(scheduler, "_WINDOWS_WORKER_GATE", gate)
    process, job = scheduler._spawn_worker(store, run)
    try:
        assert process.wait(timeout=10) == 0
        actual = json.loads(target.read_text())
        assert Path(actual["prefix"]).resolve() == Path(sys.prefix).resolve()
        assert actual["pytest"]
    finally:
        scheduler._stop_worker(process, job)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signal handler lifecycle")
def test_worker_shutdown_restores_handler_and_avoids_non_main_thread(tmp_path, monkeypatch):
    calls = []
    async def check(*_):
        calls.append(True)
    monkeypatch.setattr(jobs_worker, "run_check", check)
    previous = signal.getsignal(signal.SIGTERM)
    handler = lambda *_: None
    signal.signal(signal.SIGTERM, handler)
    try:
        asyncio.run(jobs_worker.run_with_shutdown(None, {}))
        assert signal.getsignal(signal.SIGTERM) is handler
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(lambda: asyncio.run(jobs_worker.run_with_shutdown(None, {}))).result(timeout=5)
        assert signal.getsignal(signal.SIGTERM) is handler
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert calls == [True, True]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX cancellation of the actual ReAct stream")
def test_cancelled_real_worker_is_unknown_even_when_react_emits_an_error(tmp_path, monkeypatch):
    from test_persistent_jobs_worker import environment
    from agent.cli.jobs_commands import _model_key

    arrived, release = threading.Event(), threading.Event()
    class WaitingProvider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.send_response(200)
            if "messages" not in body:
                self.end_headers()
                self.wfile.write(b'{"tokens":[]}')
                return
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            arrived.set()
            release.wait(10)
    provider = ThreadingHTTPServer(("127.0.0.1", 0), WaitingProvider)
    thread = threading.Thread(target=provider.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / "home"
    for key, value in environment(home, provider).items():
        monkeypatch.setenv(key, value)
    store = JobStore(home)
    store.add(name="fixture", prompt="wait", original_request="fixture", model=_model_key("fixture"),
              workdir=tmp_path, next_at=1000, idle_seconds=3, timeout_seconds=10)
    run = store.claim_due(now=1000)
    try:
        scheduler.execute(store, run)
        assert arrived.is_set(), store.run(run["id"])
        assert store.run(run["id"])["state"] == "unknown"
        with store.run_lock(run["id"]):
            pass
    finally:
        release.set()
        provider.shutdown()
        provider.server_close()
        thread.join(timeout=5)
