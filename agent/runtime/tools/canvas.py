"""The canvas tools: make a slide deck or export it (canvas), look at one slide (canvas_look).

Only the desktop draws a canvas, so the tools are offered only where the desktop renders pages for this
backend (agent/runtime/canvas.py, specs/canvas.md). The content is written with the ordinary file tools;
these make the canvas, hand the model the template's guide, and ask the desktop for a picture of a slide
or a print of the deck. Looking writes nothing, so it is a tool of its own that needs no approval.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from agent.runtime.canvas import (
    GUIDES, MANIFEST, SLIDES_FILE, TYPES, CanvasError, canvas_root, create, read_manifest, slide_count,
)
from agent.runtime.page_snapshot import PageSnapshot, PageSnapshotError
from agent.runtime.tool_failure import ToolFailure

from .registry import ToolDef, ToolRegistry

Render = Callable[..., Awaitable[PageSnapshot]]

DESCRIPTION = (
    "Slide decks the user sees beside the conversation on the desktop. create makes <name>.canvas/ "
    "and returns how to write its slides; edit its slides.html with the file tools (the panel follows "
    "every write); export prints the deck to a PDF in the canvas's exports folder; types lists the canvas "
    "types. Look at a slide with canvas_look."
)
LOOK_DESCRIPTION = (
    "Attach a picture of one slide of a canvas, drawn as the desktop shows it, to look at what was "
    "written: overflow, crowding, contrast, images."
)


def register_canvas_tool(registry: ToolRegistry, *, workdir: str | Path, render: Render) -> None:
    root = Path(workdir).resolve()

    def folder_of(value: str) -> Path:
        path = Path(value.strip()).expanduser()
        return canvas_root(path if path.is_absolute() else root / path)

    async def canvas_look(canvas: str, slide: int = 1) -> str | ToolFailure:
        return await canvas_tool("look", canvas=canvas, slide=slide)

    async def canvas_tool(action: str, type: str = "slides", title: str = "", path: str = "", canvas: str = "",
                          slide: int = 1, format: str = "pdf") -> str | ToolFailure:
        try:
            if action == "types":
                return "\n".join(f"- {name}: {text}" for name, text in TYPES.items())
            if action == "create":
                folder = await asyncio.to_thread(create, root, type, title, path)
                starter = hashlib.sha256((folder / SLIDES_FILE).read_bytes()).hexdigest()
                # The first line names the canvas: the desktop opens it from this line.
                return (f"Canvas: {folder / MANIFEST}\nEdit: {folder / SLIDES_FILE}\nAssets: {folder / 'assets'}\n"
                        f"{SLIDES_FILE} holds one title slide (sha256 {starter}): replace it whole with "
                        f"write_file(overwrite=true, expected_sha256=\"{starter}\"), or edit it.\n\n"
                        + GUIDES[type].format(slides=folder / SLIDES_FILE, assets=folder / "assets"))
            if not canvas.strip():
                raise CanvasError("Name the canvas: its folder or its canvas.json.")
            folder = await asyncio.to_thread(folder_of, canvas)
            manifest = await asyncio.to_thread(read_manifest, folder)
            count = await asyncio.to_thread(slide_count, folder)
            if action == "look":
                if count == 0:
                    raise CanvasError(f'{folder / SLIDES_FILE} has no <section class="slide">.')
                if not 1 <= slide <= count:
                    raise CanvasError(f"The deck has {count} slides; slide is 1 to {count}.")
                picture = await render(folder / MANIFEST, slide=slide)
                payload = {
                    "success": True, "type": "image_attachment", "image_paths": [str(picture.image_path)],
                    "canvas": str(folder / MANIFEST), "slide": slide, "slides": count, "question": "", "detail": "auto",
                    "message": (f"Slide {slide} of {count} was rendered as the desktop shows it and its picture "
                                "attached for the next model turn. Inspect the actual pixels before answering."),
                }
                if picture.script_error:
                    payload["page_script_error"] = picture.script_error
                    payload["message"] += f" A script raised an error while it was drawn: {picture.script_error}"
                return json.dumps(payload, ensure_ascii=False)
            if action == "export":
                if format != "pdf":
                    raise CanvasError("Only PDF export is available so far.")
                if count == 0:
                    raise CanvasError(f'{folder / SLIDES_FILE} has no <section class="slide">.')
                stem = folder.name[: -len(".canvas")] or "deck"
                target = folder / "exports" / f"{stem}.pdf"
                printed = await render(folder / MANIFEST, export="pdf", target=str(target), timeout=150)
                return json.dumps({"success": True, "file": str(printed.image_path), "format": "pdf", "slides": count,
                                   "canvas": str(folder / MANIFEST), "title": manifest.get("title", "")}, ensure_ascii=False)
            raise CanvasError(f"Unknown action {action!r}.")
        except CanvasError as exc:
            return ToolFailure(code="canvas_error", message=str(exc), retryable=True)
        except PageSnapshotError as exc:
            # What stops a canvas from being drawn is nearly always its content; asking again draws it the same.
            return ToolFailure(code="canvas_rendering_failed", message=str(exc), retryable=False)

    registry.register(ToolDef(
        name="canvas",
        description=DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["types", "create", "export"]},
                "type": {"type": "string", "enum": sorted(TYPES), "description": "create: the canvas type.", "default": "slides"},
                "title": {"type": "string", "description": "create: the deck's title."},
                "path": {"type": "string", "description": "create: where to make it, inside the workspace; default <title>.canvas."},
                "canvas": {"type": "string", "description": "export: the canvas folder or its canvas.json."},
                "format": {"type": "string", "enum": ["pdf"], "description": "export: the file format.", "default": "pdf"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        fn=canvas_tool, risk="write", group="canvas", sandboxed=False,
        # A print of a long deck takes a while; the desktop gives up on its own before this.
        timeout=180,
    ))
    registry.register(ToolDef(
        name="canvas_look",
        description=LOOK_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "canvas": {"type": "string", "description": "The canvas folder or its canvas.json."},
                "slide": {"type": "integer", "minimum": 1, "description": "The slide, from 1.", "default": 1},
            },
            "required": ["canvas"],
            "additionalProperties": False,
        },
        fn=canvas_look, risk="read", idempotent=True, group="canvas", sandboxed=False, timeout=60,
    ))
