"""Canvases: a work the user sees beside the conversation, drawn from a template (specs/canvas.md).

A canvas is a folder ``<name>.canvas`` holding ``canvas.json`` (its type and title), the content its
template renders (slides: ``slides.html``) and ``assets/``. The model writes the content with the
ordinary file tools; the desktop renders it with the template's own code, in the same isolated frame
as a page, and draws pictures and prints of it when asked. ``canvas.json`` is how a canvas is named
everywhere: the folder alone is not a file the desktop opens.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

MANIFEST = "canvas.json"
SUFFIX = ".canvas"
SLIDES_FILE = "slides.html"
# The desktop renders slides.html whole or not at all.
MAX_SLIDES_BYTES = 2 * 1024 * 1024

TYPES = {
    "slides": "a 16:9 slide deck: one 1920x1080 slide per <section>, shown as a list and presented "
              "full-screen; exported as PDF or PPTX",
}

SLIDES_GUIDE = """# Slides canvas

The deck is {slides}. Edit it with the file tools; the panel beside the conversation follows every
write. After a change, look at the slides you touched with canvas_look(canvas=..., slide=N)
and fix what overflows or crowds before you say the deck is done. Export with
canvas(action="export", canvas=..., format="pdf").

## Format
- slides.html is a fragment: optional <style> blocks, then one <section class="slide"> per slide, in
  order. No <html>, <head> or <body>.
- A slide is 1920x1080 CSS pixels and never scrolls: what does not fit is cut off. Body text is 40px;
  plan about six short lines, one table or one chart per slide.
- Write the deck in the user's language.

## Layouts: one class next to "slide"
- title: a centred <h1> and a subtitle <p>. For the opening slide.
- section: a divider with one large <h1> (and an optional <p>).
- (none): a content slide: <h1>, then <p>, <ul>/<ol>, <table> or <figure>.
- columns: <h1>, then <div class="columns"> with two or three children side by side.
- image: one full-bleed <img>, an optional <h1> over it and <p class="caption">.
- quote: a <blockquote> and <p class="by">.

## Parts
- <p class="lead"> for a larger opening line; .muted, .small, .accent; <strong>.
- Key numbers: <div class="stats"><div><b>42%</b><span>what it counts</span></div>...</div>
- Charts: inline <svg viewBox="0 0 W H" width=".." height=".."> drawn by hand; use currentColor and
  var(--deck-accent) so they follow the theme.
- Images: save them in {assets} and write <img src="assets/name.png">. Nothing loads from the
  network; a web address shows as a broken image.
- Speaker notes: <aside class="notes">...</aside> inside a slide. Hidden on the slide; kept for
  the presenter and exported to PPTX notes.

## Theme
In a <style> block, set any of these on :root to restyle the whole deck:
--deck-bg, --deck-text, --deck-muted, --deck-accent, --deck-accent-text, --deck-font,
--deck-heading-font. For one slide: <section class="slide" style="--deck-bg:#0f172a;--deck-text:#f8fafc">.

## Rules
- No script is needed. One that is added runs isolated: no network, storage or pop-ups.
- One idea per slide; titles under about 40 characters; prefer a chart or numbers to a paragraph.
"""

GUIDES = {"slides": SLIDES_GUIDE}


class CanvasError(ValueError):
    """A path that is not a canvas, or a request the canvas cannot satisfy."""


def canvas_root(path: Path) -> Path:
    """The canvas folder that ``path`` (the folder, its canvas.json, or a file inside it) belongs to."""
    path = path.resolve()
    for candidate in (path, *path.parents):
        if candidate.name.endswith(SUFFIX) and (candidate / MANIFEST).is_file():
            return candidate
    raise CanvasError(f"{path} is not in a canvas: a canvas is a folder named <name>{SUFFIX} with a {MANIFEST}.")


def read_manifest(root: Path) -> dict:
    try:
        manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CanvasError(f"{root / MANIFEST} cannot be read: {exc}") from None
    if not isinstance(manifest, dict) or manifest.get("type") not in TYPES:
        raise CanvasError(f"{root / MANIFEST} names no known canvas type ({', '.join(TYPES)}).")
    return manifest


def _slug(title: str) -> str:
    slug = re.sub(r"[^\w一-鿿-]+", "-", title.strip(), flags=re.UNICODE).strip("-._")
    return slug[:40] or "canvas"


def create(workdir: Path, kind: str, title: str, path: str = "") -> Path:
    """Make a new canvas of ``kind`` inside ``workdir``; return its folder."""
    if kind not in TYPES:
        raise CanvasError(f"Unknown canvas type {kind!r}; types: {', '.join(TYPES)}.")
    title = title.strip() or "Untitled"
    workdir = workdir.resolve()
    if path.strip():
        target = Path(path.strip()).expanduser()
        target = (target if target.is_absolute() else workdir / target).resolve()
        if not target.name.endswith(SUFFIX):
            target = target.with_name(target.name + SUFFIX)
        if target.exists():
            raise CanvasError(f"{target} already exists; edit it, or choose another path.")
    else:
        stem, number = _slug(title), 1
        target = workdir / f"{stem}{SUFFIX}"
        while target.exists():
            number += 1
            target = workdir / f"{stem}-{number}{SUFFIX}"
    if target != workdir and workdir not in target.parents:
        raise CanvasError("A canvas is made inside the workspace.")
    (target / "assets").mkdir(parents=True)
    manifest = {"type": kind, "title": title, "version": 1, "entry": SLIDES_FILE,
                "created": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    (target / MANIFEST).write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (target / SLIDES_FILE).write_text(starter_slides(title), encoding="utf-8")
    return target


def starter_slides(title: str) -> str:
    safe = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f'<section class="slide title">\n  <h1>{safe}</h1>\n  <p class="muted">{time.strftime("%Y-%m-%d")}</p>\n</section>\n'


_SLIDE = re.compile(r"<section\b[^>]*\bclass\s*=\s*(['\"])(?:[^'\"]*\s)?slide(?:\s[^'\"]*)?\1", re.IGNORECASE)


def slide_count(root: Path) -> int:
    """How many slides slides.html holds."""
    try:
        source = (root / SLIDES_FILE).read_text(encoding="utf-8")
    except OSError:
        return 0
    return len(_SLIDE.findall(source))
