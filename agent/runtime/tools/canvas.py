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
import logging
import re
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

from agent.runtime.canvas import (
    DOC_FILE, GUIDES, MANIFEST, SLIDES_FILE, TYPES, CanvasError, build_pptx, canvas_root, create, inside_canvas, read_manifest,
    record_version,
    slide_count, slide_parts,
)
from agent.runtime.page_snapshot import PageSnapshot, PageSnapshotError
from agent.runtime.tool_failure import ToolFailure

from .registry import ToolDef, ToolRegistry

Render = Callable[..., Awaitable[PageSnapshot]]
logger = logging.getLogger(__name__)

DESCRIPTION = (
    "Slide decks and documents the user sees beside the conversation on the desktop. create makes "
    "<name>.canvas/ and returns how to write it: a deck's slides.html with the file tools, a document's "
    "doc.md with the doc_* tools; the panel follows every write and keeps every version. export writes a deck "
    "to its exports folder as a PDF (selectable text) or a PPTX (one picture per slide, with the speaker "
    "notes). types lists the canvas types. Look at a slide with canvas_look."
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
                if type == "doc":
                    # The first line names the canvas: the desktop opens it from this line.
                    return (f"Canvas: {folder / MANIFEST}\nEdit: {folder / DOC_FILE}\nAssets: {folder / 'assets'}\n\n"
                            + GUIDES[type].format(doc=folder / DOC_FILE, assets=folder / "assets"))
                starter = hashlib.sha256((folder / SLIDES_FILE).read_bytes()).hexdigest()
                return (f"Canvas: {folder / MANIFEST}\nEdit: {folder / SLIDES_FILE}\nAssets: {folder / 'assets'}\n"
                        f"{SLIDES_FILE} holds one title slide (sha256 {starter}): replace it whole with "
                        f"write_file(overwrite=true, expected_sha256=\"{starter}\"), or edit it.\n\n"
                        + GUIDES[type].format(slides=folder / SLIDES_FILE, assets=folder / "assets"))
            if not canvas.strip():
                raise CanvasError("Name the canvas: its folder or its canvas.json.")
            folder = await asyncio.to_thread(folder_of, canvas)
            manifest = await asyncio.to_thread(read_manifest, folder)
            if manifest["type"] == "doc" and action in {"look", "export"}:
                raise CanvasError(f"A document canvas is read as text ({folder / DOC_FILE}) and exported with doc_export.")
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
                if format not in {"pdf", "pptx"}:
                    raise CanvasError("Export to pdf or pptx.")
                if count == 0:
                    raise CanvasError(f'{folder / SLIDES_FILE} has no <section class="slide">.')
                stem = folder.name[: -len(".canvas")] or "deck"
                if format == "pptx":
                    return await export_pptx(folder, stem, count, str(manifest.get("title", "")))
                target = inside_canvas(folder, "exports") / f"{stem}.pdf"
                printed = await render(folder / MANIFEST, export="pdf", target=str(target), timeout=150)
                return json.dumps({"success": True, "file": str(printed.image_path), "format": "pdf", "slides": count,
                                   "canvas": str(folder / MANIFEST), "title": manifest.get("title", "")}, ensure_ascii=False)
            raise CanvasError(f"Unknown action {action!r}.")
        except CanvasError as exc:
            return ToolFailure(code="canvas_error", message=str(exc), retryable=True)
        except PageSnapshotError as exc:
            # What stops a canvas from being drawn is nearly always its content; asking again draws it the same.
            return ToolFailure(code="canvas_rendering_failed", message=str(exc), retryable=False)

    async def export_pptx(folder: Path, stem: str, count: int, title: str) -> str:
        try:
            import pptx  # noqa: F401
        except ImportError:
            raise CanvasError("PPTX export needs python-pptx (Astra's documents extra), which is not installed.") from None
        # A link out of the canvas is refused before anything is drawn there or removed from there.
        pictures_folder = inside_canvas(folder, "exports", ".slides")
        drawn = await render(folder / MANIFEST, export="slides", target=str(pictures_folder), timeout=60 + 10 * count)
        try:
            pictures = [path for path in drawn.paths if path.parent == pictures_folder and path.is_file()]
            if len(pictures) != count:
                raise CanvasError(f"The desktop drew {len(pictures)} of the deck's {count} slides.")
            target = inside_canvas(folder, "exports") / f"{stem}.pptx"
            await asyncio.to_thread(build_pptx, pictures, slide_parts(folder), target, title)
        finally:
            await asyncio.to_thread(shutil.rmtree, pictures_folder, True)
        return json.dumps({"success": True, "file": str(target), "format": "pptx", "slides": count,
                           "canvas": str(folder / MANIFEST), "title": title,
                           "note": "Each slide is one picture: its text cannot be edited in PowerPoint."}, ensure_ascii=False)

    def keep_version(name: str, args: dict, result: dict, tool_def) -> None:
        """After a write into a canvas, keep its content as a version: what the model changed can be gone back to."""
        if tool_def is None or tool_def.risk != "write" or result.get("error"):
            return None
        named = [args.get(key) for key in ("path", "file_path")]
        named += re.findall(r"^\*\*\* (?:Add|Update) File: (.+)$", str(args.get("patch") or ""), re.MULTILINE)
        for value in {str(item).strip() for item in named if isinstance(item, str) and ".canvas" in item}:
            try:
                folder = folder_of(value)
                record_version(folder, "write")
            except CanvasError:
                continue
            except Exception:
                logger.exception("Could not keep a version of the canvas %s", value)
        return None

    registry.hooks.on_after_tool(keep_version)

    registry.register(ToolDef(
        name="canvas",
        description=DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["types", "create", "export"]},
                "type": {"type": "string", "enum": sorted(TYPES), "description": "create: the canvas type.", "default": "slides"},
                "title": {"type": "string", "description": "create: the title."},
                "path": {"type": "string", "description": "create: where to make it, inside the workspace; default <title>.canvas."},
                "canvas": {"type": "string", "description": "export: the canvas folder or its canvas.json."},
                "format": {"type": "string", "enum": ["pdf", "pptx"], "description": "export: the file format.", "default": "pdf"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        fn=canvas_tool, risk="write", group="canvas", sandboxed=False,
        # A print or a PPTX of a long deck takes a while; the desktop gives up on its own before this.
        timeout=600,
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
