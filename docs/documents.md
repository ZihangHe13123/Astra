# Documents

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/documents.md)

The `documents` tool group writes a Markdown document section by section, handles review comments and exports Word, PDF or HTML copies. The document stays an ordinary `.md` file that you can open and edit in any editor while Astra works on it.

[Document format](#document-format) · [Tools](#tools) · [Safe editing](#safe-editing) · [Review comments](#review-comments) · [Export](#export) · [Desktop preview](#desktop-preview) · [Terminal view](#terminal-view) · [Limits](#limits)

## Document format

```markdown
# Proposal

One paragraph that states the main point.

<!-- astra:section id="background" -->
## 1 Background

Written text…

<!-- astra:section id="data" status="pending" intent="Participants, protocol, labels" -->
## 2 Data collection

*Pending: Participants, protocol, labels*
```

- The first level-1 heading is the title; the text under it is the lead (section id `_lead`).
- Every other `#` heading starts a section, which runs until the next heading of the same or a higher level, so a level-2 section contains its level-3 sub-sections. Writing a section replaces only its own text, up to its first sub-section; removing a section also removes its sub-sections.
- The HTML comment above a heading keeps the section's id stable when its heading changes. Headings you add yourself without a marker get an id derived from the heading text.
- A section Astra has not written yet shows a visible `*Pending: …*` line.
- Markers and comments are invisible in rendered Markdown. Nothing inside fenced code blocks is treated as a heading, marker or comment.

## Tools

With the default stable tool manifest (`PROMPT_CACHE_STABLE_TOOLS=1`) these four tools are always offered. With progressive routing, Astra activates the group for requests about documents, reports, proposals, theses or exports, or on demand.

| Tool | Purpose |
| --- | --- |
| `doc_create` | Create a document with a title, lead and one pending placeholder per section |
| `doc_outline` | Show sections with id, level, status, word count and hash, and the open review comments; optionally one section's text |
| `doc_edit` | `write` a section's own text (optionally its heading; sub-sections stay), `add` a section before or after another or at the end, `remove` a section with its sub-sections, or `resolve_comment` with a note |
| `doc_export` | Export Word, PDF or HTML |

The earlier per-action tools (`doc_write_section`, `doc_add_section`, `doc_remove_section`, `doc_comments`, `doc_resolve_comment`) still run for old transcripts but are no longer offered to the model.

## Safe editing

- Filling an untouched pending section needs no extra argument.
- Rewriting a written section, or removing a section whose own text or sub-sections were written, needs the `expected_hash` from the latest outline. The hash covers the sub-sections, so editing a sub-section changes its parent's hash. If you changed that section in the meantime, the call fails with `section_changed` and your text stays; Astra rereads it and redoes the edit.
- Every write is atomic, and Astra refuses it if the file changed while the edit was prepared.
- Writes create file checkpoints, so `checkpoint_restore` and the turn change history work as they do for the file tools.
- Paths follow the same [filesystem policy](execution.md) as the file tools: the workspace is writable, and other locations need approval.

## Review comments

Write instructions anywhere in the document as HTML comments:

```markdown
The pilot covers three people. <!-- @astra: say who the three people are -->
```

Then ask Astra to handle the comments. It reads them from `doc_outline`, edits the affected sections and resolves each comment with `doc_edit`. A comment that needs your decision stays open with an explanation.

## Export

`doc_export` writes the copy next to the document unless you name an output path. Section markers and comments are left out. Pending placeholders stay in the copy and are reported as warnings.

| Format | Engine |
| --- | --- |
| Word (`docx`) | pandoc when installed, otherwise the built-in python-docx exporter |
| PDF | The Word file converted by LibreOffice |
| HTML | pandoc when installed, otherwise markdown-it-py |

Install the built-in exporter once:

```text
astra setup --extra documents
```

In a development checkout, `uv sync --inexact --extra documents` does the same. pandoc is optional. Set `ASTRA_PANDOC` or `ASTRA_SOFFICE` when pandoc or LibreOffice is not on `PATH`. The `engine` argument (`auto`, `python-docx` or `pandoc`) chooses the engine explicitly.

The built-in Word exporter supports:

- headings (the single title becomes the Word *Title* style), paragraphs, bold, italic, strikethrough and inline code;
- links, and bullet, numbered, nested and task lists;
- tables with column alignment, code blocks, quotes and horizontal rules;
- PNG, JPEG, GIF, BMP, TIFF and WebP images.

On macOS, headless LibreOffice may not see system fonts, so the PDF conversion links them into its temporary profile and, when the text contains Chinese, Japanese or Korean, sets an installed CJK font (Hiragino Sans GB first) in the temporary Word copy. The exported Word file itself is unchanged.

Pass a `.docx` file as `template` to reuse its styles, for example a course or company template. Without a template, the page size is A4 (`paper` switches to Letter).

Images must be local files inside the allowed roots. Remote images are not downloaded, and SVG is not supported in Word; both are replaced by their alt text with a warning. Raw HTML, footnotes and math are not converted by the built-in exporter; pandoc handles more Markdown.

## Desktop preview

In the [desktop GUI](gui.md), the **Files** panel opens the document Astra is writing and refreshes it after every doc tool result.

- The preview renders Markdown with its images and lists open review comments above the text.
- **Source** switches to the raw text.
- Closing the panel, switching tabs or pressing **← Back** stops following documents for the rest of that session.

## Terminal view

In the terminal UI, each doc tool result is one transcript line, for example `文档 report.md · 写入「结果」 · 1 节待写 · /doc 12 查看`. The activity dock labels the tools `DOC NEW`, `DOC WRITE`, `DOC EXPORT` and so on.

| Command | Effect |
| --- | --- |
| `/doc` | Open the newest document in the details view |
| `/doc <result>` | Open the document of a numbered tool result |
| `/doc open [result]` | Open the document with the system's default app |

The details view reads the current file. It hides section markers, highlights headings, review comments and pending sections, and aligns tables by display width. While it is open, it follows new writes to the same document. **Ctrl+O** opens the newest tool result as before; use `/doc` when another tool ran last.

## Limits

- Only `#` headings create sections; underlined (setext) headings do not.
- The document tools accept files up to 2 MB.
- This is not a real-time multi-user editor. Share the exported copy, or keep the Markdown file in your own synced folder.
