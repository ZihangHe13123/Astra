"""Reasoning settings migrate safely and change only the provider effort."""

import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from agent.runtime.llm import LLMClient, LLMConfig, OpenAICompatibleProvider
from agent.cli.mode_preferences import (
    DEFAULT_REASONING_EFFORT,
    REASONING_EFFORTS,
    apply_reasoning_effort,
    read_reasoning_effort,
    reasoning_effort_status,
    save_reasoning_effort,
    set_reasoning_effort,
)


def _settings(monkeypatch, tmp_path, payload=None):
    path = tmp_path / "settings.json"
    if payload is not None:
        path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(path))
    return path


@pytest.mark.parametrize("payload,expected", [
    (None, "high"), ({}, "high"), ({"agent_mode": "coding"}, "max"),
    ({"agent_mode": "chat"}, "high"), ({"agent_mode": []}, "high"),
    ({"reasoning_effort": "low", "agent_mode": "coding"}, "low"),
    ({"reasoning_effort": "invalid", "agent_mode": "coding"}, "high"),
    ({"reasoning_effort": []}, "high"), ({"code_mode": "code"}, "high"),
])
def test_startup_migrates_legacy_preferences_without_rewriting(monkeypatch, tmp_path, payload, expected):
    path = _settings(monkeypatch, tmp_path, payload)
    before = path.read_bytes() if path.exists() else None
    assert read_reasoning_effort() == expected
    assert apply_reasoning_effort(LLMConfig()).reasoning_effort == expected
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.parametrize("raw", ["broken json", "[]", "null"])
def test_bad_settings_use_default(monkeypatch, tmp_path, raw):
    path = _settings(monkeypatch, tmp_path)
    path.write_text(raw)
    assert read_reasoning_effort() == DEFAULT_REASONING_EFFORT


@pytest.mark.parametrize("effort", REASONING_EFFORTS)
def test_save_retires_old_keys_and_preserves_unrelated_preferences(monkeypatch, tmp_path, effort):
    path = _settings(monkeypatch, tmp_path, {
        "selected_model": "deepseek::deepseek-flash", "agent_mode": "coding",
        "code_mode": "both", "vision_tiles_enabled": True,
    })
    assert save_reasoning_effort(effort) == path
    assert read_reasoning_effort() == effort
    assert json.loads(path.read_text()) == {
        "selected_model": "deepseek::deepseek-flash", "vision_tiles_enabled": True,
        "reasoning_effort": effort,
    }


@pytest.mark.parametrize("effort", ["coding", "chat", "code", "medium", "turbo", ""])
def test_retired_or_unknown_values_are_rejected_without_writing(monkeypatch, tmp_path, effort):
    path = _settings(monkeypatch, tmp_path, {})
    with pytest.raises(ValueError, match="/mode"):
        save_reasoning_effort(effort)
    with pytest.raises(ValueError):
        apply_reasoning_effort(LLMConfig(), effort)
    assert json.loads(path.read_text()) == {}


@pytest.mark.parametrize("effort", REASONING_EFFORTS)
@pytest.mark.parametrize("budget", [1024, 16384, 384000])
def test_intensity_preserves_every_other_config_field(effort, budget):
    config = LLMConfig(model="deepseek-flash", max_tokens=budget,
                       context_limit=1000000, temperature=0.7, reasoning_effort="max")
    before = asdict(config)
    updated = apply_reasoning_effort(config, effort)
    assert updated is not config
    assert asdict(updated) == {**before, "reasoning_effort": effort}
    assert asdict(config) == before


def _client():
    def factory(config):
        provider = OpenAICompatibleProvider.__new__(OpenAICompatibleProvider)
        provider.config = config
        return provider
    return LLMClient(LLMConfig(model="deepseek-flash", max_tokens=384000,
                              context_limit=1000000), provider_factory=factory)


@pytest.mark.parametrize("effort", REASONING_EFFORTS)
def test_live_change_reaches_next_request_and_survives_model_switch(monkeypatch, tmp_path, effort):
    _settings(monkeypatch, tmp_path, {})
    llm = _client()
    before = asdict(llm.config)
    set_reasoning_effort(llm, effort)
    assert asdict(llm.config) == {**before, "reasoning_effort": effort}
    kwargs = llm.provider._completion_kwargs([{"role": "user", "content": "test"}])
    assert kwargs['reasoning_effort'] == effort
    assert kwargs['max_tokens'] == 384000
    assert read_reasoning_effort() == effort
    llm.switch_model("other-model", max_tokens=8192, context_limit=32768)
    assert "reasoning_effort" not in llm.provider._completion_kwargs([])
    assert "does not apply" in reasoning_effort_status(llm.config)
    llm.switch_model("deepseek-flash", max_tokens=384000, context_limit=1000000)
    assert llm.provider._completion_kwargs([])['reasoning_effort'] == effort


def test_failed_provider_switch_does_not_save_new_preference(monkeypatch, tmp_path):
    path = _settings(monkeypatch, tmp_path, {"reasoning_effort": "high"})
    def fail(*args, **kwargs):
        raise RuntimeError("provider unavailable")
    llm = SimpleNamespace(config=LLMConfig(), switch_model=fail)
    with pytest.raises(RuntimeError):
        set_reasoning_effort(llm, "low")
    assert json.loads(path.read_text()) == {"reasoning_effort": "high"}


def test_classic_cli_reports_and_sets_effort_without_budget_controls(monkeypatch, tmp_path, capsys):
    from agent.cli.main import handle_slash
    _settings(monkeypatch, tmp_path, {})
    agent = SimpleNamespace(llm=_client())
    for command in ["/mode low", "/mode high", "/mode xhigh", "/mode max", "/mode"]:
        asyncio.run(handle_slash(command, agent))
    output = capsys.readouterr().out
    for effort in REASONING_EFFORTS:
        assert f"Reasoning effort: {effort}" in output
    assert "max_tokens" not in output
    assert agent.llm.config.max_tokens == 384000
    assert agent.llm.config.context_limit == 1000000
    for command in ["/mode coding", "/mode chat", "/mode code native"]:
        asyncio.run(handle_slash(command, agent))
    assert capsys.readouterr().out.count("Unknown reasoning effort") == 3
    assert read_reasoning_effort() == "max"
