# Canvases

Status: slide decks (with PDF and PPTX export), documents with comments, and kept versions ship in
the desktop app ([user guide](../docs/gui.md#canvases)). The phone view, commenting on one slide,
editable PPTX, dashboards and design boards remain open.

## 1. Summary

A canvas is a work shown beside the conversation: it has a title, a type and versions, and Astra's
own template draws it from what the model writes. Decks export to PDF and PPTX, documents to Word,
PDF or HTML, and the person can comment on a passage for Astra to act on.

## 2. Why

Before canvases the desktop had cards (small isolated programs in a reply) and pages (an HTML file
the model writes, shown isolated in the side panel). A page had no identity beyond its file name, no
template, no export, no versions, and the model could only look at its first screen. Canvases add
those on top of the page machinery. Like tools ([tool-disclosure.md](tool-disclosure.md)), a
template's instructions are given only when one is made: `canvas(action="types")` lists the types in
one line each, and `create` returns the full guide for that type.

## 3. On disk

A folder `<name>.canvas/` in the workspace:

| Entry | Contents |
|---|---|
| `canvas.json` | `{type, title, version, entry, created}`; what names the canvas everywhere |
| content | slides: `slides.html`, one `<section class="slide">` per slide; document: `doc.md`, an ordinary `doc_*` document |
| `assets/` | images the content names as `assets/<file>` |
| `exports/` | PDF and PPTX exports |
| `.versions/` | kept versions of the content |

The model edits the content with the ordinary file tools (decks) or the `doc_*` tools (documents),
so hashes, diffs, checkpoints and turn changes work as for any file. The desktop recognises a canvas
by its `canvas.json`, not by the tool that wrote it.

## 4. Templates

**Slides.** A slide is 1920×1080 CSS pixels and never scrolls. Layouts are a class next to `slide`:
title, section, plain content, columns, image, quote; parts include lead text, key numbers, inline
SVG charts and speaker notes (`<aside class="notes">`). A theme is a set of CSS variables
(`--deck-bg`, `--deck-text`, `--deck-accent`, ...), for the deck or one slide. The desktop composes
the deck runtime, the slides and their images into one document in three modes:
- view: every slide scaled to the panel's width; click one to present, arrows and Space move,
  Escape returns;
- capture: one slide at its own size, for the model to look at;
- print: one slide per page.
Anything a script adds outside the slides takes no room in a capture or a print.

**Documents.** `doc.md` is written section by section with the `doc_*` tools, loaded with
`tool_search(family="documents")`. The panel shows it rendered, with its outline; open comments are
listed on top. Export is the existing `doc_export`.

## 5. Model interface

Two deferred tools in the "canvas" family, registered only for the desktop's backends:

| Tool | Action | Effect |
|---|---|---|
| `canvas` | `types` | the canvas types, one line each |
| | `create(type, title, path?)` | makes the folder and starter content; returns its paths and the type's guide (a deck's result includes the starter's SHA-256 for a whole-file overwrite) |
| | `export(canvas, format)` | a deck as `exports/<name>.pdf` (vector, selectable text) or `exports/<name>.pptx` |
| `canvas_look` | `canvas, slide` | a picture of one slide, attached like `read_image`; read-only, so it needs no approval |

`canvas` is a write tool and asks under the usual approval policy. A document canvas refuses look
and export and names `doc_export`.

## 6. Desktop

- Writing any file in a `.canvas` folder, a `doc_*` result for its `doc.md`, or a `canvas` /
  `canvas_look` result opens the canvas in the Files panel, which follows later changes.
- A slide picture is drawn at 1920×1080 in an offscreen window, in the same isolated frame as a
  page, and handed to the model at 1280×720.
- The PDF is printed from a one-time top-level print document served under the frame's content
  policy plus `sandbox allow-scripts`; the preload removes WebRTC there as in frames. A script that
  tries to navigate away is stopped and the print goes on.
- The PPTX: the desktop draws every slide at 1920×1080 into `exports/.slides/`; the backend builds a
  16:9 deck with python-pptx (the `documents` extra), one full-slide picture per slide, the slide's
  heading as the picture's description for screen readers and its notes as speaker notes, then
  removes the pictures. Text cannot be edited in PowerPoint.
- Comments: selecting a passage of a document opens a comment form. The desktop writes
  `<!-- @astra: ... -->` into `doc.md`:
  1. right after the passage when it is found as written (runs of spaces read as one), outside code
     blocks;
  2. otherwise at the end of the line that holds the passage, or its first 24 characters, once
     inline Markdown (emphasis, code, links) is removed;
  3. otherwise at the end of the document, quoting the passage.
  A comment never goes inside a heading (it goes on the line below) and two hyphens become a dash,
  so the comment cannot end early. The write is refused, and asked again, if the file changed
  meanwhile. `doc_comments` lists the comments; the model resolves each with `doc_edit`. The model
  is not told automatically; the person asks for the comments to be handled.

## 7. Versions

- `.versions/NNNN/` holds a copy of the content file and `meta.json` with `at`, `source`, `sha256`
  and `file`. The runtime and the desktop write the same format.
- Sources: `create`; `write`, recorded by an after-tool hook after any successful write-risk tool
  that names a file in the canvas, through `call_tool` too; `comment`; `restore`.
- A version equal to the last one is not kept again; the 30 newest are kept; a new number follows
  the highest folder present, unreadable ones included.
- The panel lists the versions, shows one in place of the current content, and restores it. Before
  a comment or a restore the current content is kept if no version holds it, so a restore loses
  nothing.
- Images in `assets/` are not versioned.

## 8. Security

- Slides run under the page rules: `allow-scripts` sandbox, every network request blocked by the
  content policy, an opaque origin, no storage, no pop-ups or navigation. The same holds for
  captures and prints.
- The template's code is the app's; the model's content is only data to it.
- Images are inlined as data URLs by the main process, only from the canvas's own `assets/` folder,
  without following a link out of it, up to 8 MiB each and 40 MiB in all; nothing loads from the
  network.
- Prints and slide pictures are written only to the canvas's own `exports/` folder.
- Every path of a canvas is held to the canvas folder's real location: content, the assets folder and
  each image, versions and exports reached through a link out of the folder are refused, on the
  desktop and in the backend (which checks `exports/.slides` before drawing there or removing it).
- Comments and restores are the person's acts in the desktop window, through its file
  authorization; a canvas frame cannot ask for them.

## 9. Validation

- Unit tests: `tests/test_canvas_tool.py`; `ui-gui/tests/canvas.test.ts` (composition, inlining and
  escapes, versions, export targets, print policy, comment placement, outline, automatic opening).
- `ui-gui/tests/page-smoke.mjs`, with the real desktop, a real backend and a local fake model:
  load the canvas family through `tool_search`, create a deck through `call_tool`, write three
  slides (one full-bleed image, one that tries every way out), present, look at a slide, export a
  three-page PDF and a PPTX with notes, with nothing reaching a listener; then create a document,
  add a section with `doc_edit`, comment on a passage, and restore the first version.
- Not covered yet: printing on Windows, the phone, and the quality of decks and documents written by
  real models.

## 10. Open

- Phone: read-only pages of each slide or the PDF; "comment on this slide" from the desktop.
- Editable PPTX from a constrained, structured slide format.
- Dashboard and design-board templates.
