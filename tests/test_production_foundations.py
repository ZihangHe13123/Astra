import asyncio
import base64
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest
import httpx

from agent.cli.connections import probe_profile, switch_to_profile
from agent.cli.environment import load_project_env
from agent.cli.model_catalog import discover_model_catalog
from agent.cli.models import ModelProfile, model_profiles, save_model_profile
from agent.runtime.llm import LLMClient, LLMConfig
from agent.runtime.mcp import MCPManager, mcp_tool_name
from agent.runtime.providers import ProviderRegistry
from agent.runtime.process_env import hidden_process_creationflags, mark_agent_environment
from agent.runtime.tools.policy import ToolPolicy
from agent.runtime.tools.registry import ToolDef, ToolRegistry


def run(coro):
    return asyncio.run(coro)


def test_agent_process_markers_preserve_outer_harness():
    standalone = mark_agent_environment({})
    assert standalone["AI_AGENT"] == "astra"
    assert standalone["ASTRA_AGENT"] == "true"

    nested = mark_agent_environment({"AI_AGENT": "codex"})
    assert nested["AI_AGENT"] == "codex"
    assert nested["ASTRA_AGENT"] == "true"


def test_hidden_process_flags_never_mix_detached_and_no_window():
    flags = hidden_process_creationflags(new_process_group=True)
    if os.name == "nt":
        import subprocess

        assert flags & subprocess.CREATE_NO_WINDOW
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP
        assert not flags & subprocess.DETACHED_PROCESS
    else:
        assert flags == 0


def test_provider_registry_normalizes_names_and_reports_unknown_provider():
    registry = ProviderRegistry()
    created = object()
    registry.register("OpenAI_Compatible", lambda config: created)

    assert registry.create("openai-compatible", LLMConfig()) is created
    assert registry.names == ["openai-compatible"]
    with pytest.raises(ValueError, match="Available: openai-compatible"):
        registry.create("missing", LLMConfig())


def test_failed_provider_switch_keeps_previous_client_state():
    registry = ProviderRegistry()
    current_provider = object()
    config = LLMConfig(model="current", provider="working")
    client = LLMClient(config, provider=current_provider, provider_registry=registry)

    with pytest.raises(ValueError, match="Unknown provider"):
        client.switch_model("next", provider_name="missing")

    assert client.config is config
    assert client.provider is current_provider


@pytest.mark.parametrize("url_override", [None, "", " \t ", "http://127.0.0.1:9000/v1", "  http://127.0.0.1:9000/v1\t"])
def test_yaml_model_catalog_supports_environment_and_user_overrides(tmp_path, monkeypatch, url_override):
    bundled = tmp_path / "models.yaml"
    bundled.write_text(
        """version: 1
models:
  local:
    provider: openai-compatible
    base_url: http://127.0.0.1:8000/v1
    base_url_env: TEST_LOCAL_URL
    context_limit: 4096
    capabilities: [tools]
    generation:
      temperature: 0.1
      max_tokens: 512
""",
        encoding="utf-8",
    )
    user = tmp_path / "user-models.yaml"
    monkeypatch.setenv("AGENT_MODELS_FILE", str(bundled))
    monkeypatch.setenv("AGENT_USER_MODELS_FILE", str(user))
    if url_override is None:
        monkeypatch.delenv("TEST_LOCAL_URL", raising=False)
    else:
        monkeypatch.setenv("TEST_LOCAL_URL", url_override)

    expected_url = "http://127.0.0.1:9000/v1" if (url_override or "").strip() else "http://127.0.0.1:8000/v1"
    assert model_profiles()["local"].base_url == expected_url

    save_model_profile(
        "second",
        ModelProfile("http://127.0.0.1:9100/v1", 8192, capabilities=frozenset({"streaming"})),
        user,
    )
    profiles = model_profiles()
    assert profiles["second"].context_limit == 8192
    assert profiles["second"].capabilities == frozenset({"streaming"})


@pytest.mark.parametrize("url_override", ["", " \t "])
def test_env_only_model_still_requires_a_base_url(tmp_path, monkeypatch, url_override):
    config = tmp_path / "models.yaml"
    config.write_text("models:\n  custom:\n    base_url_env: TEST_CUSTOM_URL\n", encoding="utf-8")
    monkeypatch.setenv("AGENT_MODELS_FILE", str(config))
    monkeypatch.setenv("AGENT_USER_MODELS_FILE", str(tmp_path / "absent.yaml"))
    monkeypatch.setenv("TEST_CUSTOM_URL", url_override)

    with pytest.raises(ValueError, match="Model 'custom' is missing base_url"):
        model_profiles()


def test_model_profile_api_key_uses_environment_before_resolver(monkeypatch):
    calls = []
    profile = ModelProfile(
        "http://127.0.0.1:8000/v1",
        262_144,
        api_key_env="OMLX_API_KEY",
        api_key_resolver=lambda: calls.append("resolver") or "settings-key",
    )

    monkeypatch.setenv("OMLX_API_KEY", "environment-key")
    assert profile.api_key() == "environment-key"
    assert calls == []

    monkeypatch.delenv("OMLX_API_KEY")
    assert profile.api_key() == "settings-key"
    assert calls == ["resolver"]


def test_model_profile_api_key_resolver_fails_closed():
    def broken():
        raise OSError("settings unavailable")

    profile = ModelProfile(
        "http://127.0.0.1:8000/v1",
        262_144,
        api_key_env="",
        api_key_resolver=broken,
    )

    assert profile.api_key() == ""


def test_model_profile_credential_resolver_is_not_serialized(tmp_path, monkeypatch):
    monkeypatch.delenv("OMLX_API_KEY", raising=False)
    profile = ModelProfile(
        "http://127.0.0.1:8000/v1",
        262_144,
        api_key_env="OMLX_API_KEY",
        api_key_resolver=lambda: "settings-key",
    )
    target = tmp_path / "models.yaml"

    save_model_profile("local-model", profile, target)

    saved = target.read_text(encoding="utf-8")
    assert "api_key_resolver" not in repr(profile)
    assert "api_key_resolver" not in saved
    assert "settings-key" not in saved


def test_yaml_model_catalog_treats_missing_and_null_temperature_as_provider_default(
    tmp_path, monkeypatch
):
    bundled = tmp_path / "models.yaml"
    bundled.write_text(
        """version: 1
models:
  missing:
    base_url: http://127.0.0.1:8000/v1
    context_limit: 4096
  null-value:
    base_url: http://127.0.0.1:8001/v1
    context_limit: 4096
    generation:
      temperature: null
  explicit:
    base_url: http://127.0.0.1:8002/v1
    context_limit: 4096
    generation:
      temperature: 0.8
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_MODELS_FILE", str(bundled))
    monkeypatch.setenv("AGENT_USER_MODELS_FILE", str(tmp_path / "missing-user.yaml"))

    profiles = model_profiles()

    assert profiles["missing"].temperature is None
    assert profiles["null-value"].temperature is None
    assert profiles["explicit"].temperature == 0.8


def test_invalid_user_model_override_does_not_break_bundled_catalog(tmp_path, monkeypatch):
    bundled = tmp_path / "models.yaml"
    bundled.write_text(
        "models:\n  local:\n    base_url: http://127.0.0.1:8000/v1\n    context_limit: 4096\n",
        encoding="utf-8",
    )
    user = tmp_path / "broken.yaml"
    user.write_text("models: [", encoding="utf-8")
    monkeypatch.setenv("AGENT_MODELS_FILE", str(bundled))
    monkeypatch.setenv("AGENT_USER_MODELS_FILE", str(user))

    assert list(model_profiles()) == ["local"]


def test_dynamic_model_catalog_partitions_providers_and_keeps_offline_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent.cli.model_catalog.local_omlx_provider_spec",
        lambda: None,
    )
    catalog_file = tmp_path / "models.yaml"
    catalog_file.write_text(
        """providers:
  fast:
    label: Fast · 8000
    base_url: http://host:8000/v1
    api_key_env: FAST_KEY
    context_limit: 4096
  offline:
    label: Offline · 8002
    base_url: http://host:8002/v1
    api_key_env: OFFLINE_KEY
models:
  cached-model:
    base_url: http://host:8002/v1
    api_key_env: OFFLINE_KEY
    context_limit: 8192
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_MODELS_FILE", str(catalog_file))
    monkeypatch.setenv("AGENT_USER_MODELS_FILE", str(tmp_path / "none.yaml"))
    monkeypatch.setenv("FAST_KEY", "secret")

    class Response:
        def __init__(self, url):
            self.url = url

        def raise_for_status(self):
            if ":8002/" in self.url:
                request = httpx.Request("GET", self.url)
                response = httpx.Response(502, request=request)
                raise httpx.HTTPStatusError("offline", request=request, response=response)

        def json(self):
            return {"data": [{"id": "live-model", "metadata": {"n_ctx": 16384}}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers):
            if ":8000/" in url:
                assert headers == {"Authorization": "Bearer secret"}
            return Response(url)

    monkeypatch.setattr("agent.cli.model_catalog.httpx.AsyncClient", lambda timeout: Client())
    catalog = run(discover_model_catalog())

    assert [entry.key for entry in catalog.entries] == [
        "fast::live-model",
        "offline::cached-model",
    ]
    assert catalog.resolve("fast::live-model").profile.context_limit == 16384
    assert catalog.resolve("cached-model").provider_label == "Offline · 8002"
    assert "offline" in catalog.errors


def test_static_provider_groups_work_without_dynamic_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "agent.cli.model_catalog.local_omlx_provider_spec",
        lambda: None,
    )
    catalog_file = tmp_path / "models.yaml"
    catalog_file.write_text(
        """models:
  deepseek-model:
    base_url: https://deepseek.example/v1
    api_key_env: DEEPSEEK_API_KEY
    catalog_provider: deepseek
    provider_label: DeepSeek API
    context_limit: 128000
  qwen-model:
    base_url: https://qwen.example/v1
    api_key_env: QWEN38_API_KEY
    catalog_provider: qwen38
    provider_label: Qwen 3.8 API
    context_limit: 128000
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENT_MODELS_FILE", str(catalog_file))
    monkeypatch.setenv(
        "AGENT_USER_MODELS_FILE",
        str(tmp_path / "missing-user-models.yaml"),
    )

    catalog = run(discover_model_catalog())

    assert [(entry.key, entry.provider_label) for entry in catalog.entries] == [
        ("deepseek::deepseek-model", "DeepSeek API"),
        ("qwen38::qwen-model", "Qwen 3.8 API"),
    ]
    assert catalog.errors == {}


def test_local_connection_probe_accepts_an_endpoint_without_api_key(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "local-model"}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers):
            assert url == "http://127.0.0.1:8084/v1/models"
            assert headers == {}
            return Response()

    monkeypatch.setattr("agent.cli.connections.httpx.AsyncClient", lambda timeout: Client())
    profile = ModelProfile("http://127.0.0.1:8084/v1", 8192)
    probe = run(probe_profile("local-model", profile))

    assert probe.ok is True
    assert probe.models == ("local-model",)


def test_connection_probe_can_use_the_actual_runtime_key_and_url(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"id": "runtime-model"}]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers):
            assert url == "https://runtime.example/v1/models"
            assert headers == {"Authorization": "Bearer new-runtime-key"}
            return Response()

    monkeypatch.setenv("OLD_KEY", "stale-key")
    monkeypatch.setattr("agent.cli.connections.httpx.AsyncClient", lambda timeout: Client())
    profile = ModelProfile("https://catalog.example/v1", 8192, api_key_env="OLD_KEY")
    probe = run(probe_profile(
        "runtime-model",
        profile,
        api_key="new-runtime-key",
        base_url="https://runtime.example/v1",
    ))
    assert probe.ok is True


def test_remote_profile_switch_requires_its_own_key_and_keeps_current_state(monkeypatch):
    current_config = LLMConfig(
        model="qwen",
        base_url="https://qwen.example/v1",
        api_key="qwen-key",
    )
    current_provider = object()
    agent = type("Agent", (), {})()
    agent.llm = LLMClient(current_config, provider=current_provider)
    agent.context = type("Context", (), {"max_prompt_tokens": 0})()
    profile = ModelProfile(
        "https://deepseek.example/v1",
        128_000,
        api_key_env="DEEPSEEK_API_KEY",
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        switch_to_profile(agent, "deepseek", profile, valid_models={"deepseek"})

    assert agent.llm.config is current_config
    assert agent.llm.provider is current_provider


def test_shared_launcher_reloads_installation_key_when_started_in_another_project(tmp_path, monkeypatch):
    from agent.launcher.installation import Installation, runtime_environment
    monkeypatch.setattr(os, "environ", os.environ.copy())

    source, workspace = tmp_path / "source", tmp_path / "working project"
    source.mkdir()
    workspace.mkdir()
    (source / ".env").write_text("LLM_API_KEY=current-installation-key\n")
    (workspace / ".env").write_text("LLM_API_KEY=unrelated-project-key\n")
    monkeypatch.setenv("LLM_API_KEY", "stale-parent-key")
    install = Installation(source, "source", "git", source / ".astra", source / ".sessions",
                           source / ".astra/launcher", "0.2.0")
    for name, value in runtime_environment(install, workspace).items():
        monkeypatch.setenv(name, value)
    load_project_env(source)
    assert os.environ["LLM_API_KEY"] == "current-installation-key"
    assert os.environ["SANDBOX_WORKDIR"] == str(workspace)


def test_project_env_refreshes_key_but_preserves_explicit_runtime_options(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=deepseek-key\n"
        "QWEN38_API_KEY=qwen-key\n"
        "LLM_API_KEY=new-key\n"
        "EXA_API_KEY=new-exa-key\n"
        "SANDBOX_DOCKER=true\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "old-deepseek-key")
    monkeypatch.setenv("QWEN38_API_KEY", "old-qwen-key")
    monkeypatch.setenv("LLM_API_KEY", "old-key")
    monkeypatch.setenv("EXA_API_KEY", "old-exa-key")
    monkeypatch.setenv("SANDBOX_DOCKER", "false")
    load_project_env(tmp_path)
    assert os.environ["DEEPSEEK_API_KEY"] == "deepseek-key"
    assert os.environ["QWEN38_API_KEY"] == "qwen-key"
    assert os.environ["LLM_API_KEY"] == "new-key"
    assert os.environ["EXA_API_KEY"] == "new-exa-key"
    assert os.environ["SANDBOX_DOCKER"] == "false"


def test_project_env_drops_parent_proxy_by_default(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("LLM_API_KEY=test-key\n", encoding="utf-8")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7890")

    load_project_env(tmp_path)

    assert "HTTP_PROXY" not in os.environ
    assert "https_proxy" not in os.environ


def test_project_env_uses_only_explicit_project_proxy(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "HTTPS_PROXY=http://127.0.0.1:8899\nASTRA_PROXY_MODE=auto\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.delenv("ASTRA_PROXY_MODE", raising=False)

    load_project_env(tmp_path)

    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:8899"
    assert os.environ["ASTRA_PROXY_MODE"] == "auto"


def test_project_env_migrates_shared_key_to_qwen_without_copying_it_to_deepseek(
    tmp_path, monkeypatch
):
    (tmp_path / ".env").write_text(
        "DEEPSEEK_API_KEY=\nQWEN38_API_KEY=\nLLM_API_KEY=legacy-qwen-key\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("QWEN38_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    load_project_env(tmp_path)

    assert os.environ["QWEN38_API_KEY"] == "legacy-qwen-key"
    assert os.environ.get("DEEPSEEK_API_KEY", "") == ""


def test_safe_tool_policy_requires_session_approval_for_execution():
    called = False

    async def execute():
        nonlocal called
        called = True
        return "ran"

    registry = ToolRegistry(ToolPolicy(mode="safe"))
    registry.register(ToolDef(
        "execute_demo", "demo", {"type": "object", "properties": {}}, execute,
        risk="execute", approval="on_risk",
    ))
    denied = run(registry.execute("execute_demo", {}))
    assert "ToolApprovalRequired" in denied["error"]
    assert called is False

    registry.policy.allow("execute_demo")
    allowed = run(registry.execute("execute_demo", {}))
    assert allowed["output"] == "ran"
    assert called is True


@dataclass
class FakeRemoteTool:
    name: str = "lookup/item"
    description: str = "look up an item"
    inputSchema: dict = None

    def __post_init__(self):
        self.inputSchema = {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        }


class FakeText:
    text = "result"


class FakeResult:
    content = [FakeText()]
    structuredContent = None


class FakeSession:
    async def call_tool(self, name, arguments):
        assert name == "lookup/item"
        assert arguments == {"query": "hello"}
        return FakeResult()


def test_mcp_tools_are_namespaced_and_execute_through_registry():
    assert mcp_tool_name("demo server", "lookup/item") == "mcp__demo_server__lookup_item"
    registry = ToolRegistry(ToolPolicy(mode="permissive"))
    manager = MCPManager(max_output_chars=100)
    manager._register_tools(registry, "demo server", FakeSession(), [FakeRemoteTool()], {})

    result = run(registry.execute("mcp__demo_server__lookup_item", {"query": "hello"}))
    assert result["output"] == "result"


class FakeErrorResult:
    content = [type("ErrorText", (), {"text": "Connection timed out"})()]
    structuredContent = None
    isError = True


class FakeErrorSession:
    async def call_tool(self, name, arguments):
        return FakeErrorResult()


def test_mcp_is_error_becomes_structured_tool_failure():
    registry = ToolRegistry(ToolPolicy(mode="permissive"))
    manager = MCPManager(max_output_chars=100)
    manager._register_tools(registry, "demo server", FakeErrorSession(), [FakeRemoteTool()], {"risk": "read"})

    result = run(registry.execute("mcp__demo_server__lookup_item", {"query": "hello"}))

    assert result["output"] == ""
    assert result["error_type"] == "mcp_tool_error"
    assert result["retryable"] is True
    assert result["details"]["mcp_server"] == "demo server"
    assert result["details"]["mcp_tool"] == "lookup/item"
    assert result["details"]["remote_output"] == "Connection timed out"


class FakeImage:
    type = "image"
    mimeType = "image/png"
    data = base64.b64encode(b"small-png-payload").decode("ascii")


class FakeImageResult:
    content = [FakeImage()]
    structuredContent = None
    isError = False


class FakeImageSession:
    async def call_tool(self, name, arguments):
        return FakeImageResult()


def test_mcp_image_content_becomes_astra_image_attachment(tmp_path):
    registry = ToolRegistry(ToolPolicy(mode="permissive"), artifact_dir=tmp_path)
    manager = MCPManager(max_output_chars=100)
    manager._register_tools(registry, "demo server", FakeImageSession(), [FakeRemoteTool()], {})

    result = run(registry.execute("mcp__demo_server__lookup_item", {"query": "hello"}))
    payload = json.loads(result["output"])

    assert payload["type"] == "image_attachment"
    assert payload["source"] == "mcp:demo server/lookup/item"
    image_path = Path(payload["image_paths"][0])
    assert image_path.read_bytes() == b"small-png-payload"


class _OneResultSession:
    def __init__(self, content, *, structured=None, is_error=False):
        self.result = type("Result", (), {
            "content": content, "structuredContent": structured, "isError": is_error,
        })()

    async def call_tool(self, name, arguments):
        return self.result


def _mcp_call(tmp_path, content, **result):
    registry = ToolRegistry(ToolPolicy(mode="permissive"), artifact_dir=tmp_path)
    manager = MCPManager(tmp_path / "mcp.json", max_output_chars=1000)
    manager._register_tools(
        registry, "demo server", _OneResultSession(content, **result), [FakeRemoteTool()], {"risk": "read"},
    )
    return run(registry.execute("mcp__demo_server__lookup_item", {"query": "hello"}))


class _Uri:
    """Stands in for the SDK's URL type: it prints as the address and is not JSON-serializable."""

    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value


class _Block:
    def __init__(self, **fields):
        self.__dict__.update(fields)

    def model_dump(self, **_options):
        return dict(self.__dict__)


def test_mcp_resource_blocks_reach_the_model_as_text(tmp_path):
    link = _Block(type="resource_link", uri=_Uri("file:///reports/q3.csv"), name="Q3 report",
                  description=None, mimeType="text/csv")
    embedded = _Block(type="resource", resource=_Block(
        uri=_Uri("file:///notes.md"), mimeType="text/markdown", text="first line\nsecond line"))

    result = _mcp_call(tmp_path, [FakeText(), link, embedded])

    # A URL object used to stop the whole result with "not JSON serializable".
    assert result["error"] == ""
    assert result["output"].startswith("result\n")
    assert "file:///reports/q3.csv" in result["output"] and "Q3 report" in result["output"]
    assert "file:///notes.md" in result["output"]
    assert "first line\nsecond line" in result["output"]
    assert not result.get("partial")


def test_mcp_resource_blocks_from_the_sdk_types(tmp_path):
    types = pytest.importorskip("mcp.types")
    link = types.ResourceLink(type="resource_link", uri="file:///reports/q3.csv", name="Q3 report")
    embedded = types.EmbeddedResource(type="resource", resource=types.TextResourceContents(
        uri="file:///notes.md", mimeType="text/markdown", text="first line"))

    result = _mcp_call(tmp_path, [link, embedded])

    assert result["error"] == ""
    assert "file:///reports/q3.csv" in result["output"]
    assert "file:///notes.md" in result["output"] and "first line" in result["output"]


def test_mcp_binary_content_is_named_instead_of_pasted(tmp_path):
    payload = base64.b64encode(b"x" * 600).decode("ascii")
    audio = _Block(type="audio", mimeType="audio/wav", data=payload)
    drawing = _Block(type="image", mimeType="image/svg+xml", data=payload)
    blob = _Block(type="resource", resource=_Block(
        uri=_Uri("file:///scan.pdf"), mimeType="application/pdf", blob=payload))

    result = _mcp_call(tmp_path, [FakeText(), audio, drawing, blob])

    output = result["output"]
    assert payload not in output
    assert "audio/wav" in output and "image/svg+xml" in output and "file:///scan.pdf" in output
    assert output.count("not shown") == 3
    # The bridge left content out, so the result may not be called complete.
    assert result["partial"] is True


def test_mcp_image_that_cannot_be_attached_is_reported(tmp_path):
    broken = _Block(type="image", mimeType="image/png", data="!!! not base64 !!!")

    result = _mcp_call(tmp_path, [FakeText(), broken])

    assert result["output"].startswith("result\n")
    assert "1 image(s) from this result not shown" in result["output"]
    assert result["partial"] is True
    assert not (tmp_path / "mcp-images").exists()

    kept = _mcp_call(tmp_path, [FakeImage(), broken])
    payload = json.loads(kept["output"])
    assert len(payload["image_paths"]) == 1
    assert "1 image(s) from this result not shown" in payload["text"]
    assert kept["partial"] is True


def test_mcp_structured_result_keeps_the_other_content(tmp_path):
    link = _Block(type="resource_link", uri=_Uri("https://example.test/export/7"), name="export",
                  description=None, mimeType=None)

    result = _mcp_call(tmp_path, [FakeText(), link], structured={"rows": 3})

    assert result["output"].startswith('{"rows": 3}')
    assert "https://example.test/export/7" in result["output"]


def test_mcp_error_names_the_tool_the_model_can_call(tmp_path):
    long_message = "No such table: orders. " + "x" * 2000

    result = _mcp_call(tmp_path, [type("ErrorText", (), {"text": long_message})(), FakeImage()], is_error=True)

    assert result["error"].startswith("[MCPToolError] mcp__demo_server__lookup_item: No such table: orders.")
    assert "demo server/lookup/item" not in result["error"]
    # max_output_chars is 1000 here; what was cut and the dropped image are both named.
    assert "more characters of the server's message not shown" in result["error"]
    assert result["error"].endswith("[1 image(s) in this error result not shown]")
    assert not (tmp_path / "mcp-images").exists()


def test_mcp_include_and_exclude_tools_limit_registration():
    registry = ToolRegistry(ToolPolicy(mode="permissive"))
    manager = MCPManager()
    tools = [FakeRemoteTool(name="allowed"), FakeRemoteTool(name="blocked"), FakeRemoteTool(name="other")]

    count = manager._register_tools(
        registry,
        "demo",
        FakeSession(),
        tools,
        {"include_tools": ["allowed", "blocked"], "exclude_tools": ["blocked"]},
    )

    assert count == 1
    assert registry.tool_names == ["mcp__demo__allowed"]


def test_mcp_config_validates_tool_filters():
    warnings = MCPManager._validate_config({
        "servers": {
            "demo": {
                "transport": "stdio",
                "command": "demo",
                "include_tools": "read_image",
                "exclude_tools": [""],
            }
        }
    })

    assert "servers.demo.include_tools must be a list of non-empty tool names" in warnings
    assert "servers.demo.exclude_tools must be a list of non-empty tool names" in warnings
