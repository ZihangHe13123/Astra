/** When to tell the person, with a system notification, that Astra needs them or has something for them. */

export type AttentionNotice = { title: string; body: string; runtime?: string; /** Waiting for an answer: a phone hears it at once. */ urgent?: boolean };
type Event = { type?: string; [key: string]: unknown };

/** A reply that took at least this long is worth a notification when the window is in the background. */
export const LONG_TURN_MS = 20_000;
const BODY_CHARS = 160;

function clip(text: unknown, fallback: string): string {
  const value = String(text ?? "").replace(/\s+/g, " ").trim() || fallback;
  return value.length > BODY_CHARS ? `${value.slice(0, BODY_CHARS - 1)}…` : value;
}

export class Attention {
  private busySince = new Map<string, number>();

  constructor(private readonly options: { focused: () => boolean; now?: () => number }) {}

  /** One event of a window session, after the session state took it in (``busy`` is the state after it). */
  session(runtime: string, label: string, busy: boolean, event: Event): AttentionNotice | undefined {
    const now = (this.options.now ?? Date.now)();
    const started = this.busySince.get(runtime);
    if (busy && started === undefined) this.busySince.set(runtime, now);
    if (!busy) this.busySince.delete(runtime);
    if (this.options.focused()) return undefined;
    // A channel's approval is answered in that chat, not in the window.
    if (event.type === "tool_approval_request" && !event.channel) {
      return { title: "Astra 需要你批准", body: clip(event.approval_title || event.tool_name, "一个操作在等你决定"), runtime, urgent: true };
    }
    if (event.type === "usage_notice") return { title: "今天的模型用量超过预算了", body: clip(event.message, "定时检查暂停到明天"), runtime };
    if (event.type === "user_question_request") {
      const questions = Array.isArray(event.questions) ? event.questions as Event[] : [];
      return { title: "Astra 有问题想问你", body: clip(questions[0]?.question, "回到对话看看吧"), runtime, urgent: true };
    }
    if (!busy && started !== undefined && now - started >= LONG_TURN_MS) {
      return { title: "Astra 回复好了", body: clip(label, "回到对话看看吧"), runtime };
    }
    return undefined;
  }

  /** One event of the background host: scheduled-job results it pushes to the desktop. */
  host(event: Event): AttentionNotice | undefined {
    if (event.type === "usage_notice") return { title: "今天的模型用量超过预算了", body: clip(event.message, "定时检查暂停到明天") };
    if (event.type !== "job_notification") return undefined;
    return { title: clip(event.title, "定时任务"), body: clip(event.body, "结果在定时任务收件箱里") };
  }

  forget(runtime: string): void {
    this.busySince.delete(runtime);
  }
}
