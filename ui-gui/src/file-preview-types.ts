/** Read-only previews describe the saved source; rendering never marks layout or formulas verified. */
export type DocumentCheck = { format: 'docx' | 'pptx' | 'xlsx'; status: 'passed'; parts: number; warnings: string[] };
export type SpreadsheetSheet = { name: string; rows: string[][]; truncated: boolean };
export type FilePreview = {
  path?: string; kind?: 'text' | 'image' | 'pdf' | 'spreadsheet'; text?: string; data?: string;
  truncated?: boolean; sourceVersion?: string; cached?: boolean; converted?: boolean;
  missingFonts?: string[]; checks?: DocumentCheck; sheets?: SpreadsheetSheet[]; warnings?: string[];
};
