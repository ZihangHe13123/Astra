import json
import time

import pytest

from agent.runtime.startup_profiler import StartupProfiler

pytestmark = pytest.mark.usefixtures("without_astra_home")


def test_startup_profiler_is_opt_in(tmp_path, monkeypatch):
    monkeypatch.delenv("ASTRA_PROFILE_STARTUP", raising=False)
    profiler = StartupProfiler.from_env(tmp_path)
    profiler.mark("ignored")
    assert profiler.finish() == {}
    assert not (tmp_path / ".astra" / "startup-profile.json").exists()


def test_startup_profiler_writes_phase_only_report(tmp_path, monkeypatch):
    monkeypatch.setenv("ASTRA_PROFILE_STARTUP", "1")
    profiler = StartupProfiler.from_env(tmp_path, started=time.perf_counter() - 0.01)
    profiler.mark("durable_state")
    report = profiler.finish("ready")

    stored = json.loads(
        (tmp_path / ".astra" / "startup-profile.json").read_text(encoding="utf-8")
    )
    assert [phase["name"] for phase in stored["phases"]] == ["durable_state", "ready"]
    assert stored["total_ms"] >= 0
    assert report["path"].endswith("startup-profile.json")
    assert "prompt" not in json.dumps(stored).lower()
