/** Read-only previews describe the saved source; rendering never marks layout or formulas verified. */
export type DocumentCheck = { format: 'docx' | 'pptx' | 'xlsx'; status: 'passed'; parts: number; warnings: string[] };
export type SpreadsheetSheet = { name: string; rows: string[][]; truncated: boolean };
export type FilePreview = {
  path?: string; kind?: 'text' | 'image' | 'pdf' | 'spreadsheet' | 'page'; text?: string; data?: string;
  truncated?: boolean; sourceVersion?: string; cached?: boolean; converted?: boolean;
  missingFonts?: string[]; checks?: DocumentCheck; sheets?: SpreadsheetSheet[]; warnings?: string[];
  /** A canvas shown as a page (specs/canvas.md): its title, how many slides, and the slides as the model wrote them. */
  canvas?: { title: string; slides: number; source: string };
};
