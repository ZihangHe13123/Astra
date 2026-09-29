# Local document preview and saved-file checks

The desktop Files panel can open local PDF, DOCX, PPTX and XLSX files in addition to existing Markdown, text and image previews. Successful `doc_export` results follow the exported document. Generated document paths are also listed under Files.

- PDF uses bundled PDF.js, with page navigation and extracted page text.
- DOCX/PPTX use the pinned `@deepseek-ai/libreoffice-kit` engine on a private source snapshot, then open the resulting PDF. The original is never given to the converter as an output target. Missing fonts are listed because fallback fonts can change pagination.
- XLSX displays up to 12 worksheets, 200 rows and 50 columns per sheet. It shows saved raw/cached values, labels formulas without cached results, and never recalculates. Formatting, charts, merged-cell presentation and formatted dates are not reproduced.
- Saved source identity is checked before/after reads and conversion. A visible preview checks for source changes every three seconds and on window focus. Refresh reads the new version. Dismissal, switching files/sessions and Cancel cancel the corresponding host request.
- Files outside the existing authorized roots still require the ordinary file picker. Cached results do not bypass file authorization.

Office conversion is local and serial, with at most four metadata-only queued requests, a 60-second engine deadline and at most eight cached PDFs within a 64 MiB string-storage budget. Source and PDF sizes are limited to 32 MiB. OOXML checks cap the package at 5,000 entries, 64 MiB declared expanded data and 16 MiB per XML part. ZIP64, encrypted packages, malformed directories, missing internal relationship targets and DTD/entity declarations are rejected. No external relationship is fetched. PDF parsing/rendering failures and unavailable Office engines are shown as failures with a System Open action.

## Model-visible inspection

`doc_check(path=...)` is a read-only tool in the documents group for saved DOCX/PPTX/XLSX files. It uses the existing filesystem permission policy, reopens the bounded ZIP/XML package, checks required parts and internal relationships, and reports the source SHA-256. It requires only the Python standard library.

Its `checks` fields distinguish `structure`/`package_reopen` from `application_reopen`, `layout` and `formula_recalculation`. The latter remain `not_run`: structural inspection does not prove Word/PowerPoint/Excel compatibility, pagination or business-data correctness. XLSX formula counts and missing cached values are reported separately. Visual page inspection is still required before claiming a document is ready for delivery.

## Verification

Run from the repository root:

```sh
npm --prefix ui-gui test
ASTRA_TEST_NATIVE_OFFICE=1 ui-gui/node_modules/.bin/tsx --test ui-gui/tests/native-office-preview.test.ts
npm --prefix ui-gui run build
AGENT_PYTHON=/absolute/path/to/python node ui-gui/tests/document-preview-smoke.mjs
python -m pytest tests/test_document_inspection.py tests/test_document_tools.py tests/test_document_export.py
```

The focused Electron smoke uses isolated application state and local fixtures without model requests. It checks DOCX/PPTX native conversion, PDF canvas/text/page navigation, Chinese XLSX values, missing-font/missing-formula-cache notices, source preservation, source-change refresh, cancellation and the existing file authorization boundary. Screenshots and the result record are written to `output/playwright/document-preview/`.

The current real-engine/Electron acceptance was performed on macOS Apple Silicon. Windows and other engine targets require their own native acceptance before claiming equivalent platform coverage.
