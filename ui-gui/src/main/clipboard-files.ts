import { unlink } from "node:fs/promises";

/** Only files created by this host are candidates; prior launches are unknown. */
export class ClipboardFiles {
  private candidates = new Set<string>();
  add(path: string) { this.candidates.add(path); }

  sent(command: { text?: unknown; prompt?: unknown; path?: unknown; cmd?: unknown }) {
    const text = [command.text, command.prompt, command.cmd].filter(v => typeof v === "string").join("\n");
    // Protect before dispatch, including ambiguous failures and model reads of paths.
    for (const path of this.candidates) if (command.path === path || text.includes(path)
      || text.includes(path.replaceAll('"', '\\"'))) this.candidates.delete(path);
  }

  async cleanup(attachments: Record<string, string[]>, drafts: Record<string, string> = {}) {
    const retained = new Set(Object.values(attachments).flat());
    const text = Object.values(drafts).join("\n");
    // Called only at final quit, when the renderer can no longer add references.
    // Do not scan the folder: old files may be referenced by saved transcripts.
    for (const path of this.candidates) if (!retained.has(path) && !text.includes(path)) {
      try { await unlink(path); this.candidates.delete(path); }
      catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
    }
  }
}
