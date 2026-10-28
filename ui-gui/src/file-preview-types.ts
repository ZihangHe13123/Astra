/** Read-only previews describe the saved source; rendering never marks layout or formulas verified. */
export type DocumentCheck = { format: 'docx' | 'pptx' | 'xlsx'; status: 'passed'; parts: number; warnings: string[] };
export type SpreadsheetSheet = { name: string; rows: string[][]; truncated: boolean };
export type FilePreview = {
  path?: string; kind?: 'text' | 'image' | 'pdf' | 'spreadsheet' | 'page'; text?: string; data?: string;
  truncated?: boolean; sourceVersion?: string; cached?: boolean; converted?: boolean;
  missingFonts?: string[]; checks?: DocumentCheck; sheets?: SpreadsheetSheet[]; warnings?: string[];
  /**
   * A canvas (specs/canvas.md): its title, its type, how many slides, and the content as the model wrote it.
   * A deck is shown as a page, a document as its Markdown; `version` names a kept version shown instead of the current one.
   */
  canvas?: { title: string; type: 'slides' | 'doc'; slides: number; source: string; version?: string };
};
/** A kept version of a canvas's content (agent/runtime/canvas.py writes them too): when, from what, and its hash. */
export type CanvasVersion = { id: string; at: number; source: string; sha256: string; file: string };
