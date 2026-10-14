"""Bounded native Windows wrapper phase, process-exit and pipe-EOF diagnostics."""
from __future__ import annotations

import argparse
import ctypes
from datetime import UTC, datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import traceback
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
WRAPPERS = ("scripts/activity-control.bat", "scripts/embedding-control.bat")
VARIANTS = ("original", "stdin_devnull", "noninteractive")
GATE = """import pathlib,runpy,sys,time
gate=pathlib.Path(sys.argv[1]); deadline=time.perf_counter()+10
while not gate.is_file():
    if time.perf_counter()>=deadline: sys.exit(91)
    time.sleep(.01)
if gate.read_bytes()!=b'1': sys.exit(92)
sys.argv=sys.argv[2:]
runpy.run_path(sys.argv[0],run_name='__main__')
"""


def native_clock():
    if sys.platform != "win32":
        return None
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    frequency, ticks = ctypes.c_longlong(), ctypes.c_longlong()
    kernel.QueryPerformanceFrequency.argtypes = [ctypes.POINTER(ctypes.c_longlong)]
    kernel.QueryPerformanceCounter.argtypes = [ctypes.POINTER(ctypes.c_longlong)]
    if not kernel.QueryPerformanceFrequency(ctypes.byref(frequency)) or not kernel.QueryPerformanceCounter(ctypes.byref(ticks)):
        raise ctypes.WinError(ctypes.get_last_error())
    return {"ticks": ticks.value, "frequency": frequency.value}


def point():
    return {"qpc": native_clock(), "perf_counter": time.perf_counter(), "utc": datetime.now(UTC).isoformat()}


def inherited_stdin():
    result = {"isatty": sys.stdin.isatty()}
    if sys.platform == "win32":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.argtypes = [ctypes.c_ulong]
        kernel.GetStdHandle.restype = ctypes.c_void_p
        kernel.GetFileType.argtypes = [ctypes.c_void_p]
        kernel.GetFileType.restype = ctypes.c_ulong
        kernel.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        handle = kernel.GetStdHandle(ctypes.c_ulong(-10))
        mode = ctypes.c_ulong()
        result.update(file_type=kernel.GetFileType(handle),
                      console=bool(kernel.GetConsoleMode(handle, ctypes.byref(mode))))
    return result


def terminate_and_confirm_job(job, process):
    """Observe this owned job's full tree reaching zero before the next attempt."""
    class Accounting(ctypes.Structure):
        _fields_ = [(name, ctypes.c_longlong) for name in ("user", "kernel", "period_user", "period_kernel")] + [
            (name, ctypes.c_uint32) for name in ("faults", "total", "active", "terminated")
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.TerminateJobObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel.TerminateJobObject.restype = ctypes.c_int
    kernel.QueryInformationJobObject.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
                                                ctypes.c_ulong, ctypes.c_void_p]
    kernel.QueryInformationJobObject.restype = ctypes.c_int
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    handle = ctypes.c_void_p(job.handle)
    if not kernel.TerminateJobObject(handle, 1):
        raise ctypes.WinError(ctypes.get_last_error())
    deadline = time.perf_counter() + 5
    process.wait(timeout=max(0, deadline - time.perf_counter()))
    # Job accounting retains exited processes while references remain. The
    # exited worker's native handle is ours; close its owned Handle object so
    # its destructor cannot later double-close a reused numeric handle.
    close_process = getattr(getattr(process, "_handle", None), "Close", None)
    if close_process is None:
        raise RuntimeError("Cannot release the exited worker's owned native handle")
    close_process()
    while True:
        data = Accounting()
        if not kernel.QueryInformationJobObject(handle, 1, ctypes.byref(data), ctypes.sizeof(data), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if data.active == 0:
            break
        if time.perf_counter() >= deadline:
            raise TimeoutError(f"Owned job still has {data.active} active processes after termination")
        time.sleep(.025)
    if not kernel.CloseHandle(handle):
        raise ctypes.WinError(ctypes.get_last_error())
    job.handle = 0
    return {"active_processes": 0, "total_processes": data.total,
            "limit_terminated_processes": data.terminated, "worker_handle_released": True,
            "observed": point()}


def save(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def phase_data(receipt, origin):
    text = receipt.read_text(encoding="utf-8") if receipt.exists() else ""
    rows = []
    for line in text.splitlines():
        match = re.search(r"^(\S+).* pid=(\d+) utc=(\S+) qpc=(\d+) frequency=(\d+) highres=(\S+)$", line)
        if not match:
            rows.append({"unparsed": line})
            continue
        name, pid, utc, ticks, frequency, highres = match.groups()
        row = {"phase": name, "pid": int(pid), "utc": utc, "qpc_ticks": int(ticks),
               "qpc_frequency": int(frequency), "high_resolution": highres.lower() == "true"}
        qpc = origin.get("qpc")
        if qpc and row["high_resolution"] and row["qpc_frequency"] == qpc["frequency"]:
            row["ms_since_call"] = (row["qpc_ticks"] - qpc["ticks"]) * 1000 / qpc["frequency"]
        rows.append(row)
    return {"raw": text, "events": rows}


def observe_run(command, *, directory, variant, **kwargs):
    """Keep the fixture assertions, observing exit and EOF independently before cleanup."""
    assert kwargs.pop("timeout") == 20
    assert kwargs.pop("capture_output") is True
    assert kwargs.pop("text") is True
    assert kwargs.pop("check") is False
    encoding = kwargs.pop("encoding")
    if variant == "stdin_devnull":
        kwargs["stdin"] = subprocess.DEVNULL
    origin = point()
    events, buffers, errors = [], {"stdout": bytearray(), "stderr": bytearray()}, []
    done, changed, lock = set(), threading.Event(), threading.Lock()
    process = None

    def record(name, **details):
        event = {"event": name, **point(), **details}
        event["elapsed_ms"] = (event["perf_counter"] - origin["perf_counter"]) * 1000
        with lock:
            events.append(event)
        changed.set()

    def reader(stream, name):
        first = True
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    record(name + "_eof")
                    break
                if first:
                    record(name + "_first_byte")
                    first = False
                with lock:
                    # The harmless fixture emits one short JSON line. Drain
                    # everything, but bound retained diagnostic output.
                    buffers[name].extend(chunk[:max(0, 65536 - len(buffers[name]))])
        except Exception as exc:
            with lock:
                errors.append(name + ": " + repr(exc))
            record(name + "_read_error")
        finally:
            with lock:
                done.add(name)
            changed.set()

    def wait_exit():
        try:
            if sys.platform == "win32":
                handle = getattr(process, "_handle", None)
                if handle is None:
                    raise RuntimeError("Popen native handle unavailable")
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
                kernel.WaitForSingleObject.restype = ctypes.c_ulong
                if kernel.WaitForSingleObject(ctypes.c_void_p(int(handle)), 0xFFFFFFFF) != 0:
                    raise ctypes.WinError(ctypes.get_last_error())
                code = process.poll()
            else:
                code = process.wait()
            record("cmd_exit_observed", returncode=code)
        except Exception as exc:
            with lock:
                errors.append("native_wait: " + repr(exc))
            record("native_wait_error")
        finally:
            with lock:
                done.add("process")
            changed.set()

    try:
        record("before_popen")
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, **kwargs)
        record("popen_returned", pid=process.pid)
        # subprocess.run starts its communicate timeout after Popen returns.
        deadline = time.perf_counter() + 20
        record("communicate_deadline_started", timeout_seconds=20)
        threading.Thread(target=reader, args=(process.stdout, "stdout"), daemon=True).start()
        threading.Thread(target=reader, args=(process.stderr, "stderr"), daemon=True).start()
        threading.Thread(target=wait_exit, daemon=True).start()
        timed_out = False
        while True:
            with lock:
                complete = len(done) == 3
            if complete:
                break
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                timed_out = True
                record("communicate_timeout", cmd_returncode=process.poll())
                break
            changed.wait(remaining)
            changed.clear()
        with lock:
            output = {name: bytes(value) for name, value in buffers.items()}
        if timed_out:
            raise subprocess.TimeoutExpired(command, 20, output=output["stdout"], stderr=output["stderr"])
        if errors:
            raise RuntimeError("; ".join(errors))
        return subprocess.CompletedProcess(command, process.returncode,
                                           output["stdout"].decode(encoding), output["stderr"].decode(encoding))
    finally:
        with lock:
            snapshot = {"variant": variant, "call_origin": origin, "events": list(events), "done": sorted(done),
                        "errors": list(errors), "stdout": bytes(buffers["stdout"]).decode(encoding, "replace"),
                        "stderr": bytes(buffers["stderr"]).decode(encoding, "replace"),
                        "cmd_returncode_at_observation_end": process.poll() if process is not None else None}
        snapshot["phases"] = phase_data(directory / "service-fixture-phases.log", origin)
        save(directory / "timing.json", snapshot)
        # Do not kill by PID or block on EOF after the deadline. The controller
        # owns the entire worker/cmd/PowerShell tree through its WindowsJob.


def worker(directory, wrapper, variant):
    spec = importlib.util.spec_from_file_location("astra_service_fixture", ROOT / "tests/test_auxiliary_scripts.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    installation = module._installation

    def prepare(path, name):
        root, scripts = installation(path, name)
        if variant == "noninteractive":
            copied = scripts / Path(name).name
            original = copied.read_bytes()
            assert b"powershell.exe -NoProfile " in original
            copied.write_bytes(original.replace(b"powershell.exe -NoProfile ", b"powershell.exe -NoProfile -NonInteractive "))
        return root, scripts

    module._installation = prepare
    module.subprocess = SimpleNamespace(
        run=lambda command, **kwargs: observe_run(command, directory=directory, variant=variant, **kwargs),
        TimeoutExpired=subprocess.TimeoutExpired,
    )
    result = {"wrapper": wrapper, "variant": variant, "passed": False, "started": point(),
              "inherited_stdin": inherited_stdin(),
              "inner_stdin": "DEVNULL" if variant == "stdin_devnull" else "inherited",
              "observer": "Native process handle wait and independent pipe readers"}
    try:
        module.test_windows_service_helpers_use_sibling_script_and_return_failure(directory, wrapper)
        result["passed"] = True
    except Exception as exc:
        result.update(error_type=type(exc).__name__, error=str(exc))
        traceback.print_exc()
    result["ended"] = point()
    save(directory / "fixture-result.json", result)
    return 0 if result["passed"] else 1


def attempt(directory, wrapper, variant):
    sys.path.insert(0, str(ROOT))
    from agent.runtime.process_env import hidden_process_creationflags
    from agent.sandbox.windows_job import WindowsJob

    environment = os.environ.copy()
    executable = getattr(sys, "_base_executable", sys.executable) or sys.executable
    if os.path.normcase(executable) != os.path.normcase(sys.executable):
        environment["__PYVENV_LAUNCHER__"] = sys.executable
    gate = directory / "containment-ready"
    arguments = [executable, "-c", GATE, str(gate), str(Path(__file__).resolve()),
                 "--worker", wrapper, "--variant", variant, "--output", str(directory)]
    result = {"wrapper": wrapper, "variant": variant, "started": point(), "passed": False,
              "outer_timed_out": False, "cleanup_confirmed": False}
    process = job = None
    with (directory / "worker-stdout.log").open("wb") as stdout, (directory / "worker-stderr.log").open("wb") as stderr:
        try:
            # File gate retains the real inherited stdin; no stdin pipe is
            # manufactured before comparing inherited stdin with DEVNULL.
            process = subprocess.Popen(arguments, cwd=ROOT, env=environment, stdout=stdout, stderr=stderr,
                                       creationflags=hidden_process_creationflags())
            handle = getattr(process, "_handle", None)
            if handle is None:
                raise RuntimeError("Worker native process handle unavailable")
            job = WindowsJob.create(process_handle=int(handle), memory_mb=None, cpu_seconds=None, max_processes=16)
            result["contained"] = point()
            gate.write_bytes(b"1")
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                result["outer_timed_out"] = True
        except Exception as exc:
            result["infrastructure_error"] = repr(exc)
        finally:
            result["before_cleanup"] = point()
            if process is not None:
                try:
                    if job is not None:
                        result["job_empty"] = terminate_and_confirm_job(job, process)
                    elif process.poll() is None:
                        process.kill()  # Gate was not released: no descendants.
                    process.wait(timeout=5)
                    result["cleanup_confirmed"] = True
                except Exception as exc:
                    result["cleanup_error"] = repr(exc)
                    if job is not None:
                        try:
                            job.close()  # Best effort, never counted as confirmed.
                        except Exception as close_error:
                            result["cleanup_close_error"] = repr(close_error)
                result["worker_exit_code"] = process.returncode
    result["ended"] = point()
    receipt = directory / "fixture-result.json"
    if receipt.exists():
        result["fixture"] = json.loads(receipt.read_text(encoding="utf-8"))
        result["passed"] = bool(result["fixture"]["passed"] and result.get("worker_exit_code") == 0
                                and result["cleanup_confirmed"] and not result["outer_timed_out"]
                                and not result.get("infrastructure_error"))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker", choices=WRAPPERS, help=argparse.SUPPRESS)
    parser.add_argument("--variant", choices=VARIANTS, default="original", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Native Windows required; no simulated acceptance is reported")
    if args.worker:
        return worker(args.output, args.worker, args.variant)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    cases = [(wrapper, variant) for wrapper in WRAPPERS for variant in VARIANTS]
    summary = {"inner_timeout_seconds": 20, "outer_timeout_seconds": 30, "rounds": 6,
               "expected_attempts": 36, "started": point(), "attempts": [],
               "interpretation": [
                   "Only harmless sibling fixtures run. Product wrappers remain unchanged; NonInteractive modifies only a temporary copy.",
                   "A file gate assigns WindowsJob before fixture code runs, retaining inherited stdin. No process-name or PPID cleanup; no CIM or PowerShell pre-sampling.",
                   "Every phase includes UTC and raw Stopwatch ticks/frequency; controller events use native QPC ticks/frequency for direct cross-process alignment.",
                   "Exit and EOF are independent observations. A before-exit phase is not proof of exit. Phase JSON interval includes ConvertTo-Json and PowerShell pipeline output.",
                   "The observer uses the same20s communicate deadline and exact fixture29/JSON/argument assertions; outer cleanup occurs after evidence capture, unlike subprocess.run's automatic timeout cleanup.",
                   "The worker calls the test function outside pytest fd capture. Its inherited native stdin is recorded, but is not assumed identical to full-suite capture; DEVNULL is an explicit input control.",
                   "The first attempt and every failure are retained; six fixed rotations balance variant position. This does not reset OS or PowerShell first-use caches.",
                   "Isolated repetitions do not reproduce all full-suite scheduler/load conditions; an all-pass probe does not erase the original maintenance failure.",
               ]}
    save(output / "summary.json", summary)
    for round_index in range(6):
        order = cases[round_index:] + cases[:round_index]
        for position, (wrapper, variant) in enumerate(order):
            index = len(summary["attempts"]) + 1
            directory = output / f"{index:02}"
            directory.mkdir()
            result = attempt(directory, wrapper, variant)
            summary["attempts"].append({"index": index, "round": round_index, "position": position, **result})
            save(output / "summary.json", summary)
            print(json.dumps({"index": index, "wrapper": wrapper, "variant": variant,
                              "passed": result["passed"], "cleanup_confirmed": result["cleanup_confirmed"]}), flush=True)
            if not result["cleanup_confirmed"]:
                summary["stopped_for_unconfirmed_cleanup"] = True
                save(output / "summary.json", summary)
                return 1
    summary["passed"] = all(attempt["passed"] for attempt in summary["attempts"])
    save(output / "summary.json", summary)
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
