# Documents

[Home](../README.md) · [Documentation](README.md) · [简体中文](zh-CN/documents.md)

The `documents` tool group writes a Markdown document section by section, handles review comments and exports Word, PDF or HTML copies. The document stays an ordinary `.md` file that you can open and edit in any editor while Astra works on it.

[Document format](#document-format) · [Tools](#tools) · [Safe editing](#safe-editing) · [Review comments](#review-comments) · [Export](#export) · [Desktop preview](#desktop-preview) · [Limits](#limits)

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
- Every other `#` heading starts a section, which runs until the next heading of the same or a higher level, so a level-2 section contains its level-3 sub-sections.
- The HTML comment above a heading keeps the section's id stable when its heading changes. Headings you add yourself without a marker get an id derived from the heading text.
- A section Astra has not written yet shows a visible `*Pending: …*` line.
- Markers and comments are invisible in rendered Markdown. Nothing inside fenced code blocks is treated as a heading, marker or comment.

## Tools

The group is not in the default tool list. Astra activates it for requests about documents, reports, proposals, theses or exports, or on demand.

| Tool | Purpose |
| --- | --- |
| `doc_create` | Create a document with a title, lead and one pending placeholder per section |
| `doc_outline` | Show sections with id, level, status, word count and hash; optionally one section's text |
| `doc_write_section` | Replace one section's body, and optionally its heading |
| `doc_add_section` | Insert a section before or after another, or at the end |
| `doc_remove_section` | Delete a section and its sub-sections |
| `doc_comments` | List open review comments with their section and nearby text |
| `doc_resolve_comment` | Remove a handled comment and record a note |
| `doc_export` | Export Word, PDF or HTML |

## Safe editing

- Filling an untouched pending section needs no extra argument.
- Rewriting or removing a written section needs the `expected_hash` from the latest outline. If you changed that section in the meantime, the call fails with `section_changed` and your text stays; Astra rereads it and redoes the edit.
- Every write is atomic, and Astra refuses it if the file changed while the edit was prepared.
- Writes create file checkpoints, so `checkpoint_restore` and the turn change history work as they do for the file tools.
- Paths follow the same [filesystem policy](execution.md) as the file tools: the workspace is writable, and other locations need approval.

## Review comments

Write instructions anywhere in the document as HTML comments:

```markdown
The pilot covers three people. <!-- @astra: say who the three people are -->
```

Then ask Astra to handle the comments. It lists them with `doc_comments`, edits the affected sections and resolves each comment. A comment that needs your decision stays open with an explanation.

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

## Limits

- Only `#` headings create sections; underlined (setext) headings do not.
- The document tools accept files up to 2 MB.
- This is not a real-time multi-user editor. Share the exported copy, or keep the Markdown file in your own synced folder.
