export type QuitReason = "window" | "quit";
export type QuitChoice = "quit" | "background" | "cancel";

/** Every ordinary exit entry shares one task inspection and one user decision. */
export class QuitCoordinator {
  private pending?: Promise<void>;
  constructor(private readonly options: {
    inspect: () => boolean | Promise<boolean>;
    confirm: (reason: QuitReason) => Promise<QuitChoice>;
    perform: () => Promise<void>;
    hide: () => void;
    focus: () => void;
  }) {}

  request(reason: QuitReason): Promise<void> {
    if (this.pending) { this.options.focus(); return this.pending; }
    const pending = this.decide(reason).finally(() => { if (this.pending === pending) this.pending = undefined; });
    this.pending = pending;
    return pending;
  }

  private async decide(reason: QuitReason) {
    let ongoing = true;
    try { ongoing = await this.options.inspect(); } catch { /* unknown task state requires a decision */ }
    const choice = ongoing ? await this.options.confirm(reason) : "quit";
    if (choice === "quit") await this.options.perform();
    else if (choice === "background" && reason === "window") this.options.hide();
  }
}
