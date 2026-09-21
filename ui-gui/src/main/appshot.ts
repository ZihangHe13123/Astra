/** Desktop adapter over the existing, verified native broker transport.
 * No Ink components are imported. Offers are pinned to a runtime before ACK.
 */
import { AppshotClient, type AppshotConsumer } from "../../../ui-tui/src/appshot-client.js";
import { productionWindowsAppshotDependencies } from "../../../ui-tui/src/appshot-windows.js";
import { runAppshotCommand } from "../../../ui-tui/src/appshot-commands.js";
import { appendAppshot, appshotCount, appshotSubmission, emptyDraft, freezeAppshotSubmission,
  revokeAppshots, settleAppshotSubmission, validateAppshotOffer,
  type AppshotInputOffer, type AppshotInputState } from "../../../ui-tui/src/appshot-input.js";
import type { UIEvent } from "@astra/ui-core/session-state";

export class DesktopAppshot {
  readonly client: AppshotClient;
  readonly consumer: AppshotConsumer;
  private drafts = new Map<string, AppshotInputState>();
  private published = new Map<string, string>();
  private retryAfter = 0;
  private noticeCode = "";
  private staged = new Map<string, { runtime: string; offer: AppshotInputOffer }>();
  constructor(private active: () => string, private available: (id: string) => boolean,
              private emit: (id: string, event: UIEvent) => void,
              factory?: (consumer: AppshotConsumer) => AppshotClient, private now = () => Date.now()) {
    const canStage = (request: string) => {
      const id = active();
      return !!id && available(id) && !this.get(id).pending && this.totalCount() < 4
        && !this.staged.has(request) && ![...this.drafts.values()].some(s =>
          [...s.draft.attachments, ...(s.pending?.draft.attachments || [])].some(a => a.requestId === request));
    };
    this.consumer = {
      stage: (offer, binding) => {
        if (!canStage(offer.request_id)) return false;
        try { this.staged.set(offer.request_id, { runtime: active(), offer: validateAppshotOffer(offer, binding) }); return true; }
        catch { return false; }
      },
      stageWindows: offer => {
        if (!("recipient" in offer.binding) || !canStage(offer.requestId)) return false;
        this.staged.set(offer.requestId, { runtime: active(), offer }); return true;
      },
      commit: event => {
        const entry = this.staged.get(event.request_id);
        if (!entry || entry.runtime !== active() || !available(entry.runtime)) throw new Error("attachment_rejected");
        const { offer, runtime } = entry;
        const state = this.get(runtime);
        if (state.pending || this.totalCount() >= 4 || offer.manifestPath !== event.manifest_path
          || offer.binding.instance_id !== event.broker_id || offer.binding.session_id !== event.session_id) throw new Error("attachment_rejected");
        this.drafts.set(runtime, { ...state, draft: appendAppshot(state.draft, offer) });
        this.staged.delete(event.request_id); this.publish(runtime); return this.capacity();
      },
      revoke: event => {
        this.staged.delete(event.requestID);
        for (const [id, state] of this.drafts) { this.drafts.set(id, revokeAppshots(state, new Set([event.requestID]))); this.publish(id); }
      },
      disconnect: () => {
        this.staged.clear();
        for (const [id, state] of this.drafts) { this.drafts.set(id, revokeAppshots(state)); this.publish(id); }
        return this.capacity();
      },
    };
    this.client = factory ? factory(this.consumer) : new AppshotClient({ consumer: this.consumer,
      ...(process.platform === "win32" ? { windowsDeps: productionWindowsAppshotDependencies() } : {}) });
    this.client.on("change", () => {
      if (this.client.state.connection === "connected") { this.retryAfter = 0; this.noticeCode = ""; }
      this.update();
      for (const id of this.drafts.keys()) if (id !== active()) this.publish(id);
    });
    this.client.on("notice", (code: string) => {
      this.noticeCode = code;
      if (this.client.state.connection === "disconnected") this.retryAfter = this.now() + 30_000;
      // Transport health is replaceable status, never a new conversation message.
      if (active()) this.publish(active());
    });
  }
  get(id: string): AppshotInputState { return this.drafts.get(id) || { draft: emptyDraft() }; }
  private totalCount() { return [...this.drafts.values()].reduce((sum, state) => sum + appshotCount(state), 0); }
  capacity() {
    const id = this.active(); const state = this.get(id);
    return { appshotCount: this.totalCount(), canAccept: !!id && this.available(id) && !state.pending && this.totalCount() < 4 };
  }
  update() {
    const capacity = this.capacity(); this.client.updateDraft(this.client.activityNS, capacity.appshotCount, capacity.canAccept);
    if (this.active()) this.publish(this.active());
  }
  activity() {
    const id = this.active();
    if ((!id || this.available(id)) && (this.client.state.connection !== "disconnected" || this.now() >= this.retryAfter)) {
      if (this.client.state.connection === "disconnected") this.retryAfter = this.now() + 30_000;
      this.client.recordInput();
    }
    this.update();
  }
  private publish(id: string) {
    const state = this.get(id);
    const event = { type: "gui_appshot", broker: this.client.state, notice: this.noticeCode,
      attachments: state.draft.attachments.map(a => ({ id: a.requestId, label: a.label })),
      pending: state.pending ? { submission_id: state.pending.submissionId, status: state.pending.status } : null };
    const signature = JSON.stringify(event);
    if (this.published.get(id) !== signature) { this.published.set(id, signature); this.emit(id, event); }
  }
  prepare(id: string, command: UIEvent): UIEvent {
    if (command.type !== "message") return command;
    const state = this.get(id);
    if (state.pending) throw new Error("上一条 Appshot 尚未确认。使用 /appshot pending status 或 pending discard。");
    if (!state.draft.attachments.length) return command;
    if (command.text.trimStart().startsWith("/")) throw new Error("请先移除 Appshot 附件，再执行斜杠命令。");
    let draft = emptyDraft(state.draft.nextNumber);
    draft.text = command.text + "\n";
    for (const offer of state.draft.attachments) draft = appendAppshot(draft, offer);
    const submission = appshotSubmission(draft, command.submission_id);
    this.drafts.set(id, freezeAppshotSubmission({ draft }, command.submission_id));
    this.publish(id); this.update();
    return { ...command, text: submission.text, appshots: submission.appshots,
      appshot_session_id: submission.appshotSessionId, appshot_broker_id: submission.appshotBrokerId };
  }
  handle(id: string, event: UIEvent) {
    const state = this.get(id);
    if (event.type === "gui_disconnected") {
      for (const [request, entry] of this.staged) if (entry.runtime === id) this.staged.delete(request);
      for (const a of state.draft.attachments) this.client.release(a.requestId);
      this.drafts.set(id, revokeAppshots(state)); this.publish(id); this.update(); return;
    }
    if (!state.pending || state.pending.submissionId !== event.submission_id) return;
    const status = event.type === "message_accepted" ? "accepted" : event.type === "message_rejected" ? "rejected" : event.type === "submission_status" ? event.status : "pending";
    if (!["accepted", "rejected", "unknown"].includes(status)) return;
    if (status === "accepted") for (const a of state.pending.draft.attachments) this.client.release(a.requestId);
    this.drafts.set(id, settleAppshotSubmission(state, event.submission_id, status)); this.publish(id); this.update();
  }
  remove(id: string, request: string) {
    const state = this.get(id);
    this.drafts.set(id, revokeAppshots(state, new Set([request]))); this.client.release(request); this.publish(id); this.update();
  }
  forget(id: string) {
    const state = this.get(id);
    for (const attachment of state.draft.attachments) this.client.release(attachment.requestId);
    // Pending captures may already be part of a saved message; forgetting local
    // bookkeeping must not revoke their authority or delete their artifacts.
    for (const [request, entry] of this.staged) if (entry.runtime === id) this.staged.delete(request);
    this.drafts.delete(id); this.published.delete(id); this.update();
  }
  async command(id: string, text: string): Promise<string> {
    if (text === "/appshot pending discard") {
      const state = this.get(id);
      if (state.pending) {
        for (const a of state.pending.draft.attachments) this.client.release(a.requestId);
        this.drafts.set(id, settleAppshotSubmission(state, state.pending.submissionId, "accepted")); this.publish(id); this.update();
      }
      return "已丢弃本地待确认附件。这不会撤销已发送的请求。";
    }
    await this.client.start();
    return (await runAppshotCommand(text === "/appshot" ? "/appshot status" : text, this.client)) || "Appshot: unsupported command";
  }
}
