import asyncio
import json
from io import BytesIO
from pathlib import Path

from PIL import Image

from agent.core.msg import ContentBlock, Msg
from agent.runtime.llm import _messages_for_capabilities
from agent.runtime.react import ReActAgent
from agent.runtime.tools.image_search import image_candidates, register_image_search, write_gallery
from agent.runtime.tools.registry import ToolRegistry
from agent.runtime.tools.web import register_web_tools
from agent.runtime.tools.search_image_reader import SearchImageError


def test_candidate_provenance_dedup_and_malformed_urls():
    data = {"results": [
        {"title": "first", "url": "https://source.example/a", "extras": {"imageLinks": ["https://cdn.example/a.jpg", "javascript:alert(1)"]}, "image": "https://cdn.example/a.jpg"},
        {"title": "duplicate", "url": "https://source.example/b", "image": "https://cdn.example/a.jpg"},
        {"url": "https://user:pass@source.example/", "image": "https://cdn.example/b.jpg"},
        {"url": "https://source.example/c", "extras": {"imageLinks": "wrong shape"}, "image": "https://cdn.example/c.jpg"},
        {"url": "https://source.example/d", "image": "https://cdn.example/\nx.jpg"},
    ]}
    images = image_candidates(data, 10)
    assert [i["image_url"] for i in images] == ["https://cdn.example/a.jpg", "https://cdn.example/c.jpg"]
    assert images[0]["source_url"] == "https://source.example/a"
    assert image_candidates(data, 1)[0]["id"] == images[0]["id"]
    assert not image_candidates({"results": "bad"}, 10)


def test_public_filter_cache_and_missing_gallery_regeneration(tmp_path):
    async def scenario():
        calls = []
        async def search(query, limit):
            calls.append(query)
            return {"results": [
                {"title": "private", "url": "https://source.example/", "image": "http://127.0.0.1/a.jpg"},
                {"title": "safe", "url": "https://source.example/", "image": "https://cdn.example/a.jpg"},
            ]}, None
        async def public(url):
            return "127.0.0.1" not in url
        registry = ToolRegistry(artifact_dir=tmp_path)
        register_image_search(registry, search, public)
        first = json.loads((await registry.execute("search_images", {"query": "garden"}))["output"])
        assert first["success"] and len(first["images"]) == 1
        assert Path(first["gallery_path"]).is_file()
        again = json.loads((await registry.execute("search_images", {"query": "garden"}))["output"])
        assert again == first and len(calls) == 1
        Path(first["gallery_path"]).unlink()
        regenerated = json.loads((await registry.execute("search_images", {"query": "garden"}))["output"])
        assert Path(regenerated["gallery_path"]).is_file()
        assert len(calls) == 2
    asyncio.run(scenario())


def test_failure_empty_results_and_safe_html(tmp_path):
    async def scenario():
        async def unavailable(query, limit):
            return None, "EXA_API_KEY is not configured"
        async def public(url):
            return True
        registry = ToolRegistry(artifact_dir=tmp_path)
        register_image_search(registry, unavailable, public)
        failure = json.loads((await registry.execute("search_images", {"query": "garden"}))["output"])
        assert failure["success"] is False and "EXA_API_KEY" in failure["error"]
        async def empty(query, limit):
            return {"results": []}, None
        registry = ToolRegistry(artifact_dir=tmp_path)
        register_image_search(registry, empty, public)
        result = json.loads((await registry.execute("search_images", {"query": "garden"}))["output"])
        assert result["images"] == [] and "gallery_path" not in result
    asyncio.run(scenario())
    path = write_gallery(tmp_path, '<script>alert(1)</script>', [{
        "id": "img_test", "title": '<img onerror="alert(1)">',
        "image_url": 'https://cdn.example/a.jpg?q="x"', "source_url": "https://source.example/",
    }])
    body = path.read_text()
    assert "<script>" not in body and '<img onerror=' not in body
    assert "&lt;script&gt;" in body and "&quot;" in body
    assert "default-src 'none'" in body and 'loading="lazy"' in body


def test_exa_request_uses_existing_credentials_and_image_extras(monkeypatch, tmp_path):
    import agent.runtime.tools.web as web
    posted = []
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"results": []}
    class Client:
        def __init__(self, **kwargs): pass
        async def post(self, url, **kwargs):
            posted.append((url, kwargs))
            return Response()
        async def aclose(self): pass
    async def public(url): return True
    monkeypatch.setenv("EXA_API_KEY", "test-only-key")
    monkeypatch.setenv("EXA_API_URL", "https://exa.example")
    monkeypatch.setattr(web.httpx, "AsyncClient", Client)
    monkeypatch.setattr(web, "_is_safe_public_url", public)
    registry = ToolRegistry(artifact_dir=tmp_path)
    register_web_tools(registry, None, "exa")
    result = asyncio.run(registry.execute("search_images", {"query": "garden", "max_results": 4}))
    assert not result["error"]
    assert posted[0][0] == "https://exa.example/search"
    assert posted[0][1]["json"]["contents"] == {"extras": {"imageLinks": 3}}
    assert posted[0][1]["headers"]["x-api-key"] == "test-only-key"
    assert "web" in registry.select_groups("找图，日式庭院参考图")
    assert registry.get("search_images").parallel_safe
    assert registry.get("search_web").parallel_safe
    assert registry.get("web_extract").parallel_safe
    assert not registry.get("extract_url").parallel_safe
    visible = {item["function"]["name"] for item in registry.to_openai_tools()}
    assert {"search_web", "fetch_url", "web_extract", "search_images"} <= visible
    assert not {"extract_url", "search_status"} & visible


def test_image_noise_filter_preview_failures_and_source_diversity(tmp_path):
    async def scenario():
        async def search(query, limit):
            return {"results": [
                {"url": "https://source.example/a", "image": "https://cdn.example/a.jpg", "extras": {"imageLinks": ["https://cdn.example/alternate.jpg", "https://cdn.example/login/code.png"]}},
                {"url": "https://source.example/b", "image": "https://cdn.example/broken.jpg"},
                {"url": "https://source.example/c", "image": "https://cdn.example/logo.svg"},
            ]}, None
        async def public(url): return True
        checked = []
        async def preview(url):
            checked.append(url)
            return "broken" not in url
        registry = ToolRegistry(artifact_dir=tmp_path)
        register_image_search(registry, search, public, preview_available=preview)
        result = json.loads((await registry.execute("search_images", {"query": "garden"}))["output"])
        assert [x["image_url"] for x in result["images"]] == ["https://cdn.example/a.jpg"]
        assert not any("login" in x or ".svg" in x for x in checked)
    asyncio.run(scenario())


def _png(path):
    Image.new("RGB", (8, 6), (248, 22, 13)).save(path)
    return path


def test_selected_image_loading_partial_failures_cache_and_unknown_ids(tmp_path):
    async def scenario():
        downloads = []
        async def search(query, limit):
            return {"results": [
                {"url": "https://source.example/a", "image": "https://cdn.example/a.png"},
                {"url": "https://source.example/b", "image": "https://cdn.example/b.png"},
            ]}, None
        async def public(url): return True
        async def load(url):
            downloads.append(url)
            if url.endswith("b.png"):
                raise SearchImageError("Image download returned HTTP 403.")
            return _png(tmp_path / "actual.png")
        registry = ToolRegistry(artifact_dir=tmp_path)
        register_image_search(registry, search, public, load_image=load)
        async def call(name, args):
            return json.loads((await registry.execute(name, args))["output"])
        candidates = await call("search_images", {"query": "red object"})
        ids = [item["id"] for item in candidates["images"]]
        assert candidates["visual_status"] == "not_inspected" and not downloads
        for invalid in [["https://arbitrary.example/image.png"], [ids[0], ids[0]], ids * 3, []]:
            failed = await call("read_search_images", {"image_ids": invalid})
            assert failed["success"] is False
        assert not downloads
        loaded = await call("read_search_images", {"image_ids": ids, "question": "Is the object red?"})
        assert loaded["type"] == "image_attachment" and loaded["success"]
        assert loaded["visual_status"] == "awaiting_model_inspection"
        assert [item["id"] for item in loaded["images"]] == [ids[0]]
        assert loaded["failures"] == [{"id": ids[1], "error": "Image download returned HTTP 403."}]
        assert ids[0] in loaded["image_labels"][0]
        assert "Is the object red?" == loaded["question"]
        await call("read_search_images", {"image_ids": [ids[0]]})
        assert len(downloads) == 2
        Path(loaded["image_paths"][0]).unlink()
        regenerated = await call("read_search_images", {"image_ids": [ids[0]]})
        assert Path(regenerated["image_paths"][0]).is_file() and len(downloads) == 3
        assert (await call("search_images", {"query": "red object"}))["visual_status"] == "not_inspected"

    asyncio.run(scenario())


def test_web_registration_loads_exa_images_with_existing_http_transport(monkeypatch, tmp_path):
    import httpx
    import agent.runtime.tools.web as web
    requests = []
    clients = []
    actual_client = httpx.AsyncClient
    picture = BytesIO()
    Image.new("RGB", (12, 8), "red").save(picture, format="PNG")

    def respond(request):
        requests.append((request.method, str(request.url)))
        if request.method == "POST":
            assert request.headers["x-api-key"] == "test-only-key"
            return httpx.Response(200, json={"results": [{"url": "https://source.example/",
                                                        "image": "https://cdn.example/a.png"}]})
        if request.method == "HEAD":
            return httpx.Response(200, headers={"content-type": "image/png", "content-length": "4096"})
        return httpx.Response(200, content=picture.getvalue())

    def make_client(**kwargs):
        client = actual_client(transport=httpx.MockTransport(respond))
        clients.append(client)
        return client

    async def public(url): return True
    monkeypatch.setenv("EXA_API_KEY", "test-only-key")
    monkeypatch.setenv("EXA_API_URL", "https://exa.example")
    monkeypatch.setattr(web.httpx, "AsyncClient", make_client)
    monkeypatch.setattr(web, "_is_safe_public_url", public)
    monkeypatch.setattr(web, "active_proxy_for_url", lambda _: None)

    async def scenario():
        registry = ToolRegistry(artifact_dir=tmp_path)
        register_web_tools(registry, None, "exa")
        result = json.loads((await registry.execute("search_images", {"query": "red"}))["output"])
        loaded = json.loads((await registry.execute("read_search_images", {
            "image_ids": [result["images"][0]["id"]],
        }))["output"])
        assert loaded["success"] and Path(loaded["image_paths"][0]).read_bytes() == picture.getvalue()
        assert len(clients) == 1  # Search, preview and image loading reuse the same transport.
        assert requests == [("POST", "https://exa.example/search"), ("HEAD", "https://cdn.example/a.png"),
                            ("GET", "https://cdn.example/a.png")]
        for client in clients:
            await client.aclose()

    asyncio.run(scenario())


def test_search_pixels_and_matching_ids_reach_next_model_turn_without_persisting_bytes(tmp_path):
    class FakeLLM:
        calls = 0
        seen = None
        candidate_id = ""

        async def chat_stream(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                name, arguments = "search_images", {"query": "red picture"}
            elif self.calls == 2:
                text = next(msg["content"] for msg in reversed(messages) if msg["role"] == "tool")
                result, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
                self.candidate_id = result["images"][0]["id"]
                assert result["visual_status"] == "not_inspected"
                name, arguments = "read_search_images", {"image_ids": [self.candidate_id], "question": "Check actual color"}
            else:
                self.seen = messages
                yield {"type": "done", "content": "The pixels are red.", "usage": None}
                return
            yield {"type": "tool_calls", "calls": [{"id": f"call-{self.calls}", "name": name,
                   "arguments": json.dumps(arguments)}], "content": "", "usage": None}

    async def scenario():
        async def search(query, limit):
            return {"results": [{"title": "misleading blue title", "url": "https://source.example/",
                                 "image": "https://cdn.example/image.png"}]}, None
        async def public(url): return True
        async def load(url): return _png(tmp_path / "pixels.png")
        registry = ToolRegistry(artifact_dir=tmp_path)
        register_image_search(registry, search, public, load_image=load)
        llm = FakeLLM()
        agent = ReActAgent("agent", llm, registry, max_iterations=4)
        events = [event async for event in agent.reply_stream(Msg(content=[ContentBlock.text("搜图并核对颜色")]))]
        assert events[-1]["type"] == "done" and llm.calls == 3
        messages = [m for m in llm.seen if isinstance(m.get("content"), list)
                    and any(p.get("type") == "image_url" for p in m["content"])]
        assert len(messages) == 1
        parts = messages[0]["content"]
        labels = " ".join(part.get("text", "") for part in parts)
        assert llm.candidate_id in labels and "https://source.example/" in labels
        assert "Check actual color" in labels
        import base64
        url = next(part["image_url"]["url"] for part in parts if part["type"] == "image_url")
        with Image.open(BytesIO(base64.b64decode(url.split(",", 1)[1]))) as pixels:
            assert pixels.getpixel((0, 0)) == (248, 22, 13)
        assert "data:image" not in json.dumps(agent.context.messages)
        assert "data:image" not in json.dumps(events)
        text_only = _messages_for_capabilities(messages, frozenset())
        assert "data:image" not in json.dumps(text_only)
        assert "text-only" in json.dumps(text_only) and "Do not pretend to see" in json.dumps(text_only)

    asyncio.run(scenario())
