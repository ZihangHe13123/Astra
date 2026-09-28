"""Astra OpenAI-compatible API server.

Exposes the ReAct agent through a standard /v1/chat/completions endpoint
so external tools (AstrBot, Hermes, curl) can use Astra as a drop-in
LLM provider with full tool capabilities.

Usage:
    python -m agent.cli.api_server                  # 127.0.0.1:8900
    ASTRA_API_PORT=9000 python -m agent.cli.api_server
    ASTRA_API_KEY=<secret> ASTRA_API_HOST=0.0.0.0 python -m agent.cli.api_server

ASTRA_API_KEY, when set, requires Authorization: Bearer <secret> on every
endpoint. Non-loopback bindings require a key. Browser-origin requests also
require configured authentication, including on loopback.

Endpoints:
    GET  /v1/models              — list available models
    POST /v1/chat/completions    — chat completion (streaming + non-streaming)
    GET  /health                 — health check
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import sys
import time
import uuid
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

# Keep direct script launches working as well as the documented module entry.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

if __name__ == "__main__":
    from agent.launcher.locking import protect_backend
    protect_backend()

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from agent.cli.context_index_preferences import load_context_index_preferences
from agent.cli.sessions import (
    SessionNameError,
    api_session_path,
    validate_api_session_id,
)
from agent.runtime.context import AgentContext
from agent.runtime.context_compressor import ContextCompressor
from agent.runtime.context_index import create_context_index_broker
from agent.runtime.context_index.workspace import resolve_workspace
from agent.runtime.tools.context_index import register_context_index_tools

if TYPE_CHECKING:
    from agent.runtime.react import ReActAgent
    from agent.runtime.session_recall import SessionRecall

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
logger = logging.getLogger("astra.api")

# ---------------------------------------------------------------------------
# Globals — populated during startup
# ---------------------------------------------------------------------------
_agent: ReActAgent | None = None
_agent_lock = asyncio.Lock()
_sessions: dict[str, AgentContext] = {}
_session_recall: SessionRecall | None = None


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            return address.ipv4_mapped.is_loopback
        return address.is_loopback
    except ValueError:
        return False


def _configured_api_key() -> str:
    return os.getenv("ASTRA_API_KEY", "").strip()


def _validate_bind_host(host: str) -> None:
    if not _is_loopback(host) and not _configured_api_key():
        raise RuntimeError("ASTRA_API_KEY is required for a non-loopback ASTRA_API_HOST")


def _authorize(request: Request) -> JSONResponse | None:
    """Reject requests before parsing input or touching model/session state."""
    key = _configured_api_key()
    if key:
        scheme, _, token = request.headers.get("authorization", "").partition(" ")
        if scheme.lower() == "bearer" and hmac.compare_digest(token.encode(), key.encode()):
            return None
        return JSONResponse(
            {"error": {"message": "Invalid or missing API key", "type": "authentication_error"}},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    # An ASGI launcher may set its own host without calling main(). Validate
    # the accepted socket address as well; do not trust forwarded headers.
    server = getattr(request, "scope", {}).get("server")
    if server and not _is_loopback(str(server[0])):
        return JSONResponse(
            {"error": {"message": "API authentication is required for this binding", "type": "server_error"}},
            status_code=503,
        )
    if "origin" in request.headers:
        return JSONResponse(
            {"error": {"message": "Browser requests require API authentication", "type": "authentication_error"}},
            status_code=403,
        )
    return None


def _current_memory_session() -> str:
    """Return the durable session stem currently mounted on the API agent."""
    if _agent is None:
        return ""
    return Path(_agent.context.session_path).stem


def _api_session_recall():
    """Return the lazy Session Recall writer shared by API turns."""
    global _session_recall
    if _session_recall is None:
        from agent.runtime.session_recall import SessionRecall

        recall = SessionRecall()
        recall.init_db()
        _session_recall = recall
    return _session_recall


# ---------------------------------------------------------------------------
# Bootstrap — mirrors backend.py initialization
# ---------------------------------------------------------------------------
async def _bootstrap_agent():
    """Initialize the ReAct agent with full tool stack."""
    from agent.cli.backend import (
        configured_model_catalog,
        is_local_url,
        load_project_env,
        read_selected_model,
    )
    from agent.cli.vision_tile_preferences import load_vision_tiles_enabled
    from agent.runtime.llm import LLMClient, LLMConfig
    from agent.runtime.memory import MemoryStore
    from agent.runtime.prompts import DEFAULT_SYSTEM_PROMPT
    from agent.runtime.react import ReActAgent
    from agent.runtime.skills import SkillStore
    from agent.runtime.tools.registry import ToolRegistry

    load_project_env(PROJECT_ROOT)

    # Resolve model
    catalog = configured_model_catalog()
    selected = read_selected_model()
    entry = (
        catalog.resolve_persisted(selected)
        or catalog.resolve(os.getenv("LLM_MODEL", ""))
        or (catalog.entries[0] if catalog.entries else None)
    )
    if entry is None:
        raise RuntimeError("No configured model. Check models.yaml.")

    api_key = entry.profile.api_key()
    if not api_key and (is_local_url(entry.base_url) or not entry.profile.api_key_env):
        api_key = "local"
    if not api_key:
        raise RuntimeError(f"{entry.profile.api_key_env} not set for {entry.model_id}")

    llm_config = LLMConfig(
        provider=entry.profile.provider,
        model=entry.model_id,
        api_key=api_key,
        base_url=entry.base_url,
        capabilities=entry.profile.capabilities,
        **entry.profile.generation_settings(),
    )

    from agent.cli.mode_preferences import apply_reasoning_effort

    llm = LLMClient(apply_reasoning_effort(llm_config))
    tools = ToolRegistry()
    memory_store = MemoryStore()
    skill_store = SkillStore()
    context_index_broker = create_context_index_broker(
        load_context_index_preferences(), os.getcwd()
    )
    context_index_broker.start_background(memory_store.path)

    # Register core tools (lighter than full backend — no sandbox/docker)
    from agent.runtime.tools.code import register_code_tools
    from agent.runtime.tools.documents import register_document_tools
    from agent.runtime.tools.files import register_file_tools
    from agent.runtime.tools.git import register_git_tools
    from agent.runtime.tools.image import register_image_tools, select_vision_tiles_from_holder
    from agent.runtime.tools.memory import register_memory_tools
    from agent.runtime.tools.session_recall import register_session_recall_tools
    from agent.runtime.tools.skills import register_skill_tools
    from agent.runtime.tools.time import register_time_tools
    from agent.runtime.tools.web import register_web_tools
    from agent.sandbox.local import LocalSandbox

    sandbox = LocalSandbox(timeout=30, workdir=os.getcwd())
    agent_holder = {}
    register_code_tools(tools, sandbox)
    register_file_tools(tools, workdir=os.getcwd(), sandbox=sandbox)
    register_document_tools(tools)
    register_git_tools(tools, workdir=os.getcwd())
    register_time_tools(tools)
    register_memory_tools(tools, memory_store, session_id=_current_memory_session)
    register_skill_tools(tools, skill_store)
    register_session_recall_tools(tools)
    register_context_index_tools(tools, context_index_broker)
    register_web_tools(tools, sandbox)
    register_image_tools(
        tools,
        workdir=os.getcwd(),
        select_tiles=lambda tile_set_id, tile_ids: select_vision_tiles_from_holder(
            agent_holder,
            tile_set_id,
            tile_ids,
        ),
    )

    # Build system prompt with persona
    system_prompt = DEFAULT_SYSTEM_PROMPT

    agent = ReActAgent(
        name="Astra",
        llm_client=llm,
        tool_registry=tools,
        system_prompt=system_prompt,
        max_iterations=30,
        memory_store=memory_store,
        skill_store=skill_store,
        context_index_broker=context_index_broker,
    )
    agent.set_vision_tiles_enabled(load_vision_tiles_enabled())
    agent_holder["agent"] = agent

    logger.info(
        "Agent ready: model=%s base_url=%s tools=%d",
        entry.model_id, entry.base_url, len(tools.tool_names),
    )
    return agent


# ---------------------------------------------------------------------------
# OpenAI-compatible handlers
# ---------------------------------------------------------------------------
async def health(request: Request) -> JSONResponse:
    denied = _authorize(request)
    if denied is not None:
        return denied
    return JSONResponse({"status": "ok", "agent": _agent is not None})


async def list_models(request: Request) -> JSONResponse:
    denied = _authorize(request)
    if denied is not None:
        return denied
    model_id = "astra"
    if _agent and hasattr(_agent, "llm"):
        model_id = getattr(_agent.llm, "model", "astra")
    return JSONResponse({
        "object": "list",
        "data": [
            {
                "id": model_id,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "astra",
            }
        ],
    })


def _extract_user_text(messages: list[dict]) -> str:
    """Extract the latest user message text from OpenAI-format messages."""
    # Use the last user message
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                # Handle content array (text + image blocks)
                texts = [
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                ]
                return "\n".join(texts)
            return str(content)
    return ""


def _validate_chat_body(body: Any) -> dict[str, Any]:
    """Validate consumed fields while allowing ordinary OpenAI history fields."""
    if not isinstance(body, dict):
        raise ValueError("JSON body must be an object")
    if not isinstance(body.get("stream", False), bool):
        raise ValueError("stream must be a boolean")
    if not isinstance(body.get("model", "astra"), str):
        raise ValueError("model must be a string")
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        raise ValueError("messages must be an array")
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("role"), str):
            raise ValueError("Each message must be an object with a string role")
        content = message.get("content")
        # Assistant tool-call messages commonly have null content.
        if content is None and message["role"] != "user":
            continue
        if isinstance(content, str):
            continue
        if not isinstance(content, list):
            raise ValueError("Message content must be text or an array of content parts")
        for block in content:
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                raise ValueError("Each content part must be an object with a string type")
            if block["type"] == "text" and not isinstance(block.get("text"), str):
                raise ValueError("Text content parts must contain a text string")
    return body


def _request_session_id(
    body: dict[str, Any],
    headers: Mapping[str, str],
) -> str | None:
    """Resolve and validate the optional Astra API session extension."""
    present_in_body = "session_id" in body
    raw = body.get("session_id") if present_in_body else headers.get("X-Astra-Session-Id")
    if raw is None and not present_in_body:
        return None
    if not isinstance(raw, str):
        raise SessionNameError("session_id must be a string")
    return validate_api_session_id(raw)


async def chat_completions(request: Request) -> Any:
    """Handle POST /v1/chat/completions."""
    denied = _authorize(request)
    if denied is not None:
        return denied
    if _agent is None:
        return JSONResponse(
            {"error": {"message": "Agent not initialized", "type": "server_error"}},
            status_code=503,
        )

    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            {"error": {"message": "Invalid JSON body", "type": "invalid_request_error"}},
            status_code=400,
        )

    try:
        body = _validate_chat_body(body)
        session_id = _request_session_id(body, request.headers)
    except (SessionNameError, ValueError) as exc:
        return JSONResponse(
            {"error": {"message": str(exc), "type": "invalid_request_error"}},
            status_code=400,
        )
    messages = body.get("messages", [])
    stream = body.get("stream", False)
    user_text = _extract_user_text(messages)

    if not user_text:
        return JSONResponse(
            {"error": {"message": "No user message found", "type": "invalid_request_error"}},
            status_code=400,
        )

    completion_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    model = body.get("model", "astra")
    session_headers = (
        {"X-Astra-Session-Id": quote(session_id, safe="._-")}
        if session_id is not None
        else {}
    )

    if stream:
        return StreamingResponse(
            _stream_response(completion_id, created, model, user_text, session_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                **session_headers,
            },
        )

    # Non-streaming: collect full response
    try:
        response_text = await _run_agent(user_text, session_id)
    except Exception as e:
        logger.exception("Agent error")
        return JSONResponse(
            {"error": {"message": str(e), "type": "server_error"}},
            status_code=500,
        )

    response_payload = {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": response_text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    if session_id is not None:
        response_payload["session_id"] = session_id
    return JSONResponse(response_payload, headers=session_headers)


def _context_for_session(session_id: str | None) -> AgentContext:
    """Build or load an isolated API context from the parked agent context."""
    if _agent is None:
        raise RuntimeError("Agent not initialized")
    if session_id is not None:
        cached = _sessions.get(session_id)
        if cached is not None:
            return cached

    template = _agent.context
    context = AgentContext(
        system_prompt=template.system_prompt,
        max_messages=template.max_messages,
        max_prompt_tokens=template.max_prompt_tokens,
        show_reasoning=template.show_reasoning,
        compaction_enabled=template.compaction_enabled,
    )
    context.persona_id = template.persona_id
    context.persona_definition_version = template.persona_definition_version
    context.persona_state_revision = template.persona_state_revision
    context.persona_active_mode = template.persona_active_mode
    context.persona_relationship_context = template.persona_relationship_context
    context.persona_affect = template.persona_affect
    context.tool_risk_provider = template.tool_risk_provider
    context.set_stable_system_suffix(getattr(template, "_stable_system_suffix", ""))
    context.set_tools_token_cost(getattr(template, "_tools_token_cost", 0))
    context.compressor = ContextCompressor(
        _agent.llm,
        hooks=getattr(_agent.tools, "hooks", None),
    )

    if session_id is not None:
        context.set_session(str(api_session_path(session_id)))
        context.load()
        _sessions[session_id] = context
    return context


async def _agent_events(user_text: str, session_id: str | None):
    """Yield one agent turn while owning context swap and persistence."""
    from agent.core.msg import ContentBlock, Msg

    async with _agent_lock:
        if _agent is None:
            raise RuntimeError("Agent not initialized")
        previous = _agent.context
        context = _context_for_session(session_id)
        previous_memory_state = (
            getattr(_agent, "memory_store", None),
            getattr(_agent, "memory_router", None),
            getattr(_agent, "memory_retainer", None),
        )
        previous_allowlist = getattr(_agent, "tool_allowlist", None)
        _agent.context = context
        if session_id is None:
            _agent.memory_store = None
            _agent.memory_router = None
            _agent.memory_retainer = None
            stateless_tools = (
                set(previous_allowlist)
                if previous_allowlist is not None
                else {
                    name
                    for name in _agent.tools.tool_names
                    if (
                        (tool := _agent.tools.get(name)) is not None
                        and tool.expose_by_default
                    )
                }
            )
            stateless_tools.discard("memory")
            _agent.tool_allowlist = stateless_tools
        msg = Msg(
            sender="user",
            role="user",
            content=[ContentBlock.text(user_text)],
        )
        recall = None
        recall_session_id = ""
        assistant_chunks: list[str] = []
        try:
            if session_id is not None:
                try:
                    recall = _api_session_recall()
                    source_key = Path(context.session_path).stem
                    workspace = resolve_workspace(Path(os.getcwd()))
                    recall_session_id = recall.get_or_create_session(
                        source_key,
                        title=source_key,
                        personality=context.persona_id,
                        workspace_key=workspace.key,
                        workspace_root=workspace.root,
                    )
                    recall.log_message(recall_session_id, "user", user_text)
                except Exception:
                    recall = None
                    logger.exception("session recall: API user logging failed")
            reply_events = _agent.reply_stream(msg)
            try:
                async for event in reply_events:
                    if event.get("type") == "chunk":
                        assistant_chunks.append(str(event.get("content") or ""))
                    yield event
                if recall is not None and recall_session_id and assistant_chunks:
                    try:
                        recall.log_message(
                            recall_session_id,
                            "assistant",
                            "".join(assistant_chunks),
                        )
                    except Exception:
                        logger.exception("session recall: API assistant logging failed")
            finally:
                await reply_events.aclose()
        finally:
            try:
                if session_id is not None:
                    context.save()
            finally:
                (
                    _agent.memory_store,
                    _agent.memory_router,
                    _agent.memory_retainer,
                ) = previous_memory_state
                _agent.tool_allowlist = previous_allowlist
                _agent.context = previous


async def _run_agent(user_text: str, session_id: str | None = None) -> str:
    """Run user text through the selected API session and collect the response."""

    chunks: list[str] = []
    events = _agent_events(user_text, session_id)
    try:
        async for event in events:
            etype = event.get("type")
            if etype == "chunk":
                chunks.append(event.get("content", ""))
            elif etype == "error":
                error_msg = event.get("message", "Unknown error")
                if chunks:
                    chunks.append(f"\n[Error: {error_msg}]")
                else:
                    chunks.append(f"[Error: {error_msg}]")
    finally:
        await events.aclose()

    return "".join(chunks) or "(no response)"


async def _stream_response(
    completion_id: str,
    created: int,
    model: str,
    user_text: str,
    session_id: str | None = None,
):
    """SSE streaming generator — yields SSE data lines directly."""
    events = _agent_events(user_text, session_id)
    try:
        async for event in events:
            etype = event.get("type")
            if etype == "chunk":
                chunk_data = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": event.get("content", "")},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk_data, ensure_ascii=False)}\n\n"
            elif etype == "error":
                error_chunk = {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": f"\n[Error: {event.get('message', '')}]"},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(error_chunk, ensure_ascii=False)}\n\n"
    finally:
        await events.aclose()

    # Final chunk with finish_reason
    final = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# App + startup
# ---------------------------------------------------------------------------
routes = [
    Route("/health", health, methods=["GET"]),
    Route("/v1/models", list_models, methods=["GET"]),
    Route("/v1/chat/completions", chat_completions, methods=["POST"]),
]

@asynccontextmanager
async def lifespan(app: Starlette):
    """Bootstrap the agent once at startup (modern Starlette lifespan)."""
    global _agent
    from agent.cli.backend import load_project_env

    load_project_env(PROJECT_ROOT)
    _validate_bind_host(os.getenv("ASTRA_API_HOST", "127.0.0.1"))
    logger.info("Bootstrapping Astra agent...")
    _agent = await _bootstrap_agent()
    logger.info("Astra API server ready.")
    yield


app = Starlette(routes=routes, lifespan=lifespan)


def main():
    import uvicorn

    from agent.cli.backend import load_project_env

    load_project_env(PROJECT_ROOT)
    port = int(os.getenv("ASTRA_API_PORT", "8900"))
    host = os.getenv("ASTRA_API_HOST", "127.0.0.1")
    _validate_bind_host(host)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    print(f"🚀 Astra API server starting on http://{host}:{port}")
    print("   POST /v1/chat/completions  — chat with Astra")
    print("   GET  /v1/models            — list models")
    print("   GET  /health               — health check")

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
