import type { UIEvent } from '@astra/ui-core/session-state';

/** One path mutation at a time. A successful stdin write is never its receipt. */
export class ResponseOperations {
  private pending?: { id: string; resolve: () => void; reject: (e: Error) => void; timer: ReturnType<typeof setTimeout> };
  constructor(private emit: (event: UIEvent) => void, private timeout = { regenerate: 15 * 60_000, select: 30_000 }) {}
  get active() { return !!this.pending; }
  run(command: UIEvent, write: (command: UIEvent) => void): Promise<void> {
    if (this.pending) return Promise.reject(new Error('回复版本操作正在进行，请稍候。'));
    return new Promise<void>((resolve, reject) => {
      this.pending = { id: command.request_id, resolve, reject, timer: setTimeout(() => {
        this.finish({ type: 'response_operation_result', request_id: command.request_id,
          error: '回复版本操作结果尚未确认。请先查看当前回复，未自动重发。' });
      }, command.type === 'response_regenerate' ? this.timeout.regenerate : this.timeout.select) };
      this.emit({ type: 'response_operation_pending', request_id: command.request_id, operation: command.type, source_ref: command.source_ref });
      try { write(command); }
      catch (error) { this.finish({ type: 'response_operation_result', request_id: command.request_id, error: String(error) }); }
    });
  }
  finish(event: UIEvent): boolean {
    const pending = this.pending;
    if (!pending || event.request_id !== pending.id) return false;
    clearTimeout(pending.timer); this.pending = undefined;
    this.emit(event);
    event.error ? pending.reject(new Error(String(event.error))) : pending.resolve();
    return true;
  }
  disconnect() {
    if (this.pending) this.finish({ type: 'response_operation_result', request_id: this.pending.id,
      error: '后端已断开，回复版本操作未确认；原回复保留，未自动重发。' });
  }
}
