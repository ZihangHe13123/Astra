import { renameSync, unlinkSync, writeFileSync } from "node:fs";
import { writeFile } from "node:fs/promises";

/** Debounced, serialized writes; a final unload flush supersedes any in-flight write. */
export class PreferenceWriter {
  private version = 0;
  private savedVersion = 0;
  private value = "";
  private timer?: ReturnType<typeof setTimeout>;
  private writing?: Promise<void>;
  constructor(private path: string, private onError: (error: unknown) => void,
    private delay = 200, private write = writeFile) {}

  schedule(value: unknown) {
    this.value = JSON.stringify(value); this.version++;
    if (this.timer) clearTimeout(this.timer);
    this.timer = setTimeout(() => { this.timer = undefined; void this.flush().catch(this.onError); }, this.delay);
  }

  async flush(): Promise<void> {
    if (this.timer) { clearTimeout(this.timer); this.timer = undefined; }
    while (this.writing) {
      try { await this.writing; }
      catch (error) { if (this.savedVersion === this.version) return; throw error; }
    }
    if (this.savedVersion === this.version) return;
    const version = this.version, value = this.value;
    const temporary = `${this.path}.${process.pid}.${version}.tmp`;
    const writing = (async () => {
      try {
        await this.write(temporary, value, { mode: 0o600 });
        // A small synchronous rename makes the generation check and commit atomic
        // with flushSync; normal file writes stay asynchronous.
        if (version === this.version) { renameSync(temporary, this.path); this.savedVersion = version; }
      } finally { try { unlinkSync(temporary); } catch { /* committed or write failed */ } }
    })();
    this.writing = writing;
    try { await writing; } finally { if (this.writing === writing) this.writing = undefined; }
    if (this.savedVersion !== this.version) await this.flush();
  }

  flushSync(value: unknown) {
    if (this.timer) { clearTimeout(this.timer); this.timer = undefined; }
    this.value = JSON.stringify(value); const version = ++this.version;
    const temporary = `${this.path}.${process.pid}.${version}.tmp`;
    try {
      writeFileSync(temporary, this.value, { mode: 0o600 });
      renameSync(temporary, this.path); this.savedVersion = version;
    } finally { try { unlinkSync(temporary); } catch { /* committed or write failed */ } }
  }
}
