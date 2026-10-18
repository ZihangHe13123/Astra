import json

import pytest

from agent.cli.persona_preferences import save_selected_persona, startup_persona
from agent.runtime.context import AgentContext
from agent.runtime.persona import PersonaAssembler, PersonaDefinition, PersonaState, parse_persona_metadata
from agent.runtime.prompts import (
    DEFAULT_PROMPT_PROFILE,
    DEFAULT_SYSTEM_PROMPT,
    get_prompt_profile,
    LEGACY_PROFILE_IDS,
    normalize_system_prompt,
    selectable_prompt_profiles,
    persona_generation_overrides,
)
from agent.runtime.react import ReActAgent
from agent.runtime.system_prompt_projection import SystemPromptProjection


def test_persona_assembler_separates_stable_definition_from_volatile_state():
    definition = PersonaDefinition(
        persona_id="test-persona",
        version=3,
        description="test",
        identity="stable agent contract",
        primary_identity="stable primary identity",
        style="stable style",
        relationship="stable relationship baseline",
        mode_overlay="mode-specific behavior",
        invariants=("never invent memory",),
        persona_identity="stable persona identity",
    )
    state = PersonaState(
        persona_id="test-persona",
        definition_version=3,
        state_revision=7,
        active_mode="daily",
        relationship_context="current relationship evidence",
        affect="calm",
    )

    prompt = PersonaAssembler().assemble(definition, state)
    metadata = parse_persona_metadata(prompt)

    assert metadata is not None
    assert metadata.persona_id == "test-persona"
    assert metadata.definition_version == 3
    assert metadata.state_revision == 7
    assert prompt.index("<persona-stable") < prompt.index("<persona-state")
    assert prompt.index("[Primary identity]") < prompt.index("[Persona identity]")
    assert prompt.index("[Persona identity]") < prompt.index("[Identity]")
    assert prompt.index("[Identity]") < prompt.index("[Style]")
    assert prompt.index("[Style]") < prompt.index("[Relationship baseline]")
    assert prompt.index("[Relationship baseline]") < prompt.index("[Invariants]")
    assert prompt.index("[Invariants]") < prompt.index("</persona-stable>")
    for heading in (
        "[Persona identity]",
        "[Identity]",
        "[Style]",
        "[Relationship baseline]",
        "[Invariants]",
    ):
        assert f"\n\n{heading}\n" in prompt
    assert "stable primary identity\n\n[Persona identity]\n" in prompt
    assert "\n\n\n[Persona identity]\n" not in prompt
    assert "stable primary identity" in prompt
    assert "stable agent contract" in prompt
    assert "stable persona identity" in prompt
    assert "current relationship evidence" in prompt


def test_public_profile_is_the_only_advertised_persona(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(tmp_path / "settings.json"))
    name, prompt, state = startup_persona()
    assert set(selectable_prompt_profiles()) == {"lyra"}, "channel versions are not offered on their own"
    assert name == DEFAULT_PROMPT_PROFILE == state.persona_id == "lyra"
    assert prompt == DEFAULT_SYSTEM_PROMPT
    assert "You are Lyra" in prompt
    assert "Do not assume the user's name" in prompt
    assert "Never invent shared experiences" in prompt
    assert "[Relationship baseline]" not in prompt
    assert "[Current relationship context]" not in prompt
    assert "[Mode overlay]" not in prompt


@pytest.mark.parametrize("legacy_id", sorted(LEGACY_PROFILE_IDS))
def test_saved_legacy_selection_migrates_without_losing_other_settings(monkeypatch, tmp_path, legacy_id):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"selected_persona": legacy_id, "selected_model": "example-model",
                               "persona_definition_version": 7, "persona_state_revision": 9}))
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(path))
    name, prompt, state = startup_persona()
    assert name == state.persona_id == "lyra"
    assert state.state_revision == 0
    assert prompt == DEFAULT_SYSTEM_PROMPT
    saved = json.loads(path.read_text())
    assert saved["selected_persona"] == "lyra"
    assert saved["selected_model"] == "example-model"
    assert saved["persona_definition_version"] == state.definition_version
    assert saved["persona_state_revision"] == 0
    before = path.read_bytes()
    assert startup_persona() == (name, prompt, state)
    assert path.read_bytes() == before


@pytest.mark.parametrize("legacy_id", sorted(LEGACY_PROFILE_IDS))
@pytest.mark.parametrize("separate_metadata", [False, True])
def test_legacy_session_retires_private_state_and_cached_projection(tmp_path, legacy_id, separate_metadata):
    path = tmp_path / "session.json"
    old_prompt = (f"<!-- agent-persona:id={legacy_id};version=7;revision=4 -->"
                  "\nRetired example persona body")
    projection = SystemPromptProjection()
    projection.project([{"role": "system", "content": old_prompt}], "migration-fixture")
    data = {"system_prompt": old_prompt,
            "persona_state": {"active_mode": "retired-mode", "relationship_context": "retired-state",
                              "affect": "retired-affect"},
            "system_prompt_projection": projection.state,
            "messages": [{"role": "user", "content": "Keep this local conversation."}]}
    if separate_metadata:
        data.update(persona_id=legacy_id, persona_definition_version=7, persona_state_revision=4)
    path.write_text(json.dumps(data))
    context = AgentContext()
    context.set_session(str(path))
    assert context.load()
    assert context.system_prompt == DEFAULT_SYSTEM_PROMPT
    assert context.persona_id == "lyra"
    assert context.persona_active_mode == "work"
    assert context.persona_relationship_context == context.persona_affect == ""
    assert context.persona_state_revision == 0
    assert context.system_projection.state == {}
    assert context.messages == data["messages"]
    wire = context.system_projection.project(
        [{"role": "system", "content": context.system_prompt}, *context.messages], "migration-fixture",
    )
    assert [m["content"] for m in wire if m["role"] == "system"] == [DEFAULT_SYSTEM_PROMPT]
    context.save()
    restored = AgentContext()
    restored.set_session(str(path))
    assert restored.load()
    assert restored.system_prompt == DEFAULT_SYSTEM_PROMPT
    assert restored.persona_relationship_context == ""
    assert restored.messages == data["messages"]


def test_unversioned_migration_uses_fingerprints_without_storing_retired_prose(monkeypatch):
    from hashlib import sha256
    import agent.runtime.prompts as prompts
    old = "An exact synthetic pre-versioning persona fixture"
    monkeypatch.setattr(prompts, "LEGACY_PROFILE_PROMPT_HASHES", {sha256(old.encode()).hexdigest()})
    assert normalize_system_prompt(old) == DEFAULT_SYSTEM_PROMPT
    assert normalize_system_prompt(old + " custom suffix") == old + " custom suffix"


def test_public_persona_state_survives_session_roundtrip(tmp_path):
    profile = get_prompt_profile("lyra")
    state = PersonaState(persona_id="lyra", definition_version=profile.version, state_revision=4,
                         active_mode="focused-work", relationship_context="user prefers direct evidence",
                         affect="calm")
    path = tmp_path / "persona-session.json"
    context = AgentContext()
    context.set_persona(state, profile.system_prompt(state))
    context.set_session(str(path))
    context.add_user("hello")
    context.save()
    restored = AgentContext()
    restored.set_session(str(path))
    assert restored.load()
    assert restored.persona_id == "lyra"
    assert restored.persona_definition_version == profile.version
    assert restored.persona_state_revision == 4
    assert restored.persona_active_mode == "focused-work"
    assert restored.persona_relationship_context == "user prefers direct evidence"
    assert restored.persona_affect == "calm"
    assert restored.system_prompt == profile.system_prompt(state)


def test_current_unversioned_form_migrates_and_custom_prompt_is_preserved(tmp_path):
    profile = get_prompt_profile("lyra")
    for raw, expected, identity in [(profile.legacy_system_prompt(), profile.system_prompt(), "lyra"),
                                    ("my custom prompt", "my custom prompt", "")]:
        path = tmp_path / f"{identity or 'custom'}.json"
        context = AgentContext(system_prompt=raw)
        context.set_session(str(path))
        context.add_user("hello")
        context.save()
        restored = AgentContext()
        restored.set_session(str(path))
        assert restored.load()
        assert restored.system_prompt == expected
        assert restored.persona_id == identity


def test_persona_settings_record_definition_version(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(path))
    save_selected_persona("lyra")
    name, prompt, state = startup_persona()
    assert name == "lyra"
    saved = json.loads(path.read_text())
    assert saved["persona_definition_version"] == state.definition_version
    assert saved["persona_state_revision"] == 0
    assert parse_persona_metadata(prompt).persona_id == name
    with pytest.raises(ValueError, match="Unknown persona"):
        save_selected_persona("unknown")


def test_sampling_defaults_do_not_override_explicit_runtime_modes():
    assert persona_generation_overrides("lyra") == {"temperature": None}
    for value in (None, "", "unknown"):
        assert persona_generation_overrides(value) is None
    agent = ReActAgent.__new__(ReActAgent)
    agent.context = AgentContext()
    agent.context.persona_id = "lyra"
    agent.generation_overrides_provider = None
    assert agent._generation_overrides() == {"temperature": None}
    agent.generation_overrides_provider = lambda: {"temperature": 0.65, "top_p": 0.9}
    assert agent._generation_overrides() == {"temperature": 0.65, "top_p": 0.9}


def test_local_profile_takes_precedence_over_public_retirement(monkeypatch, tmp_path):
    legacy_id = sorted(LEGACY_PROFILE_IDS)[0]
    config = tmp_path / "persona.local.json"
    config.write_text(json.dumps({"schema": 1, "profiles": [{
        "name": legacy_id, "description": "Local example", "persona": "Local example identity",
        "version": 9, "persona_layer": "identity", "relationship": "Local example baseline",
        "active_mode": "local-mode", "invariants": ["Local example invariant"], "temperature": 0.4,
    }]}))
    monkeypatch.setenv("ASTRA_PERSONA_FILE", str(config))
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"selected_persona": legacy_id, "persona_state_revision": 4}))
    monkeypatch.setenv("AGENT_SETTINGS_PATH", str(settings))
    before = settings.read_bytes()
    name, prompt, state = startup_persona()
    assert name == legacy_id
    assert state.state_revision == 4
    assert "Local example identity" in prompt
    assert settings.read_bytes() == before
    assert persona_generation_overrides(legacy_id) == {"temperature": 0.4}
    profile = get_prompt_profile(legacy_id)
    state = PersonaState(persona_id=legacy_id, definition_version=9, state_revision=6,
                         active_mode="local-mode", relationship_context="Local preference", affect="calm")
    original = profile.system_prompt(state)
    context = AgentContext()
    context.set_persona(state, original)
    path = tmp_path / "session.json"
    context.set_session(str(path))
    context.add_user("Synthetic conversation")
    context.save()
    restored = AgentContext()
    restored.set_session(str(path))
    assert restored.load()
    assert restored.system_prompt == original
    assert restored.persona_id == legacy_id
    assert restored.persona_state_revision == 6
    assert restored.persona_relationship_context == "Local preference"
    assert normalize_system_prompt(profile.legacy_system_prompt()) == profile.system_prompt()


def test_private_mode_prompts_are_optional_and_invalid_files_do_not_fall_back(monkeypatch, tmp_path):
    from agent.runtime.bar_mode import BAR_MODE_PROMPT, bar_mode_prompt
    from agent.runtime.persona_overrides import local_mode_prompt
    config = tmp_path / "persona.local.json"
    monkeypatch.setenv("ASTRA_PERSONA_FILE", str(config))
    assert bar_mode_prompt("atomic") == BAR_MODE_PROMPT
    config.write_text(json.dumps({"schema": 1, "mode_prompts": {
        "bar_atomic": "Example atomic contract", "bar_stream": "Example streaming contract",
        "custom": "Example local contract",
    }}))
    assert bar_mode_prompt("atomic") == "Example atomic contract"
    assert bar_mode_prompt("stream") == "Example streaming contract"
    assert local_mode_prompt("custom", "default") == "Example local contract"
    config.write_text('{"schema": 1, "mode_prompts": {"custom": false}}')
    with pytest.raises(ValueError, match="local persona configuration"):
        local_mode_prompt("custom", "default")
    config.write_text(json.dumps({"schema": 1, "profiles": [{"name": "example", "persona": "test"}]}))
    with pytest.raises(ValueError, match="Invalid local persona profile"):
        startup_persona()


def test_local_config_is_bound_to_installation_state_not_working_project(monkeypatch, tmp_path):
    from agent.runtime.persona_overrides import local_persona_path
    monkeypatch.delenv("ASTRA_PERSONA_FILE", raising=False)
    monkeypatch.setenv("ASTRA_HOME", str(tmp_path / "installation-state"))
    project = tmp_path / "other-project"
    project.mkdir()
    monkeypatch.chdir(project)
    assert local_persona_path() == tmp_path / "installation-state/persona.local.json"
