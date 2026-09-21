import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";
import { randomUUID } from "node:crypto";
import { createWriteStream, mkdirSync } from "node:fs";
import { delimiter, join } from "node:path";
import { initialSession, projectEvent, type UIEvent, type SessionState } from "@astra/ui-core/session-state";
import { createBackendEventReceiver, EventDeliveryState } from "@astra/ui-core/backend-protocol";
import { modeCommand } from "../local-mode.js";
import { createBackendHandshake } from "@astra/ui-core/backend-handshake";

export class Runtime {
  readonly id = randomUUID();
  state: SessionState;
  initialModeSession = "";
  private child?: ChildProcessWithoutNullStreams;
  private scope = `gui-${randomUUID()}`;
  private delivery = new EventDeliveryState();
  private closing = false;
  private closePromise?: Promise<void>;
  private waiters = new Set<{ resolve: () => void; reject: (error: Error) => void }>();
  private restartSession = "";
  private modelSelection?: { request_id: string; resolve: () => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> };
  private queries = new Map<string, { resolve: (value: any) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }>();
  private submissions = new Map<string, { resolve: () => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> }>();
  get isClosing() { return this.closing; }
  constructor(readonly root: string, readonly python: string, readonly workspace: string,
              readonly session: string, readonly mode: string,
              private emit: (runtime: Runtime, event: UIEvent) => void) {
    this.state = initialSession(this.id, workspace);
    this.state.session = session;
    this.start();
  }
  private start() {
    const env = { ...process.env, ASTRA_WORKSPACE: this.workspace, SANDBOX_WORKDIR: this.workspace,
      PYTHONPATH: [this.root, process.env.PYTHONPATH].filter(Boolean).join(delimiter),
      AGENT_SESSION: this.restartSession || this.session,
      ASTRA_UI_SURFACE: "gui", ASTRA_TUI_PID: String(process.pid), ASTRA_TUI_RESTART: "1", ASTRA_EVENT_SCOPE: this.scope,
      PYTHONUNBUFFERED: "1" };
    const child = spawn(this.python, ["-m", "agent.cli.backend"], { cwd: this.workspace, env, windowsHide: true });
    this.child = child;
    const logDir = process.env.AGENT_LOG_DIR || join(this.root, ".logs");
    mkdirSync(logDir, { recursive: true });
    const log = createWriteStream(join(logDir, `gui-backend-${this.id}.log`), { flags: "a", mode: 0o600 });
    // pipe applies backpressure without blocking Electron's event loop per chunk.
    child.stderr.pipe(log);
    log.on("error", error => { child.stderr.unpipe(log); child.stderr.resume(); console.error("Astra backend log:", error); });
    const handshake = createBackendHandshake(
      () => this.accept({ type: "gui_notice", message: "后端正在连接…" }),
      () => { this.accept({ type: "error", message: "后端未在 15 秒内完成协议握手，请查看诊断日志。" }); child.kill(); },
    );
    let entered = false;
    const receiver = createBackendEventReceiver(event => {
      handshake();
      const e = event as UIEvent;
      if (e.type === "backend_hello" && !e.capabilities?.includes("ui_queries")) {
        this.accept({ type: "error", message: "后端版本与 GUI 不兼容，请运行 astra setup --gui。" });
        child.kill();
        return;
      }
      if (e.type === "ui_query_result") {
        const pending = this.queries.get(e.request_id);
        if (pending) { clearTimeout(pending.timer); this.queries.delete(e.request_id); e.error ? pending.reject(new Error(e.error)) : pending.resolve(e.result); }
        return;
      }
      this.accept(e);
      if (["message_accepted", "message_rejected", "submission_status"].includes(e.type)) {
        const pending = this.submissions.get(e.submission_id);
        const status = e.type === "message_accepted" ? "accepted" : e.type === "message_rejected" ? "rejected" : e.status;
        if (pending && status !== "pending") {
          clearTimeout(pending.timer); this.submissions.delete(e.submission_id);
          if (status === "accepted") pending.resolve();
          else pending.reject(new Error(status === "unknown" ? "无法确认请求是否已接收。草稿已保留，请先查看结果，勿重复发送。" : `请求未接收：${e.code || e.reason || status}`));
        }
      }
      if (e.type === "event_replay_complete" && e.has_more) this.write({ type: "event_replay", after_cursor: e.next_cursor });
      if (e.type === "restart_ready" && !e.replayed) this.restartSession = e.session;
      if (e.type === "gui_ready" && !entered) {
        entered = true;
        if (this.mode !== "work" && !this.restartSession) {
          const command = modeCommand(this.mode, this.state.info.local_mode_info?.definition);
          if (command) this.write({ type: "command", cmd: `${command} ${this.initialModeSession || this.session}` });
          else this.accept({ type: "error", message: "此安装没有可用的本地模式，已保留通用会话。" });
        }
      }
    }, diagnostic => this.accept({ type: "gui_notice", message: `协议事件未能处理：${diagnostic.phase}/${diagnostic.event_type}` }),
    { delivery: this.delivery, onGap: after_cursor => this.write({ type: "event_replay", after_cursor }) });
    const lines = createInterface({ input: child.stdout });
    lines.on("line", receiver);
    child.on("error", error => this.accept({ type: "error", message: `无法启动后端：${error.message}` }));
    child.on("close", code => {
      handshake(); lines.close();
      for (const pending of this.queries.values()) { clearTimeout(pending.timer); pending.reject(new Error("Backend disconnected")); }
      this.queries.clear(); this.child = undefined;
      const connection = this.state.info.connection_pending;
      if (connection && this.state.info.connection_result?.request_id !== connection.request_id) {
        this.accept({ type: "connection_result", request_id: connection.request_id, error: "连接未完成：后端已断开。" });
      }
      if (this.modelSelection) {
        clearTimeout(this.modelSelection.timer); this.modelSelection.reject(new Error("模型切换未确认：后端已断开。")); this.modelSelection = undefined;
      }
      for (const pending of this.submissions.values()) { clearTimeout(pending.timer); pending.reject(new Error("后端已断开，请求是否接收尚未确认。草稿已保留，未自动重发。")); }
      this.submissions.clear();
      for (const waiter of this.waiters) waiter.reject(new Error("Backend disconnected before accepting the request"));
      this.waiters.clear();
      // Keep in sync with agent.cli.session_lifecycle.RESTART_EXIT_CODE.
      if (code === 42 && this.restartSession && !this.closing) {
        this.delivery = new EventDeliveryState(); this.start();
      } else {
        // Drop protocol bookkeeping but keep the displayed transcript, including
        // a streaming tail that may not have reached persistent session storage.
        this.delivery = new EventDeliveryState();
        this.accept({ type: "gui_disconnected", message: this.closing ? "会话后端已关闭" : `后端已退出（${code ?? "信号"}）。已显示的内容保留。` });
      }
    });
  }
  accept(event: UIEvent) {
    if (event.type === "model_selection_result") {
      const pending = this.modelSelection;
      // Ordinary /model output and late/replayed selections are not a receipt
      // for the selection currently in flight.
      if (pending && event.request_id === pending.request_id) {
        clearTimeout(pending.timer); this.modelSelection = undefined;
        event.error ? pending.reject(new Error(event.error)) : pending.resolve();
      }
      event = { ...event, type: "gui_model_result" };
    }
    this.state = projectEvent(this.state, event);
    if (this.state.status === "ready") { for (const waiter of this.waiters) waiter.resolve(); this.waiters.clear(); }
    this.emit(this, event);
  }
  async send(command: UIEvent): Promise<void> {
    if (command.type !== "connect_provider") return this.sendCommand(command);
    const pending = this.state.info.connection_pending;
    if (pending && this.state.info.connection_result?.request_id !== pending.request_id) throw new Error("模型连接正在进行，请先完成或取消。");
    // Store only public connection metadata; credentials never enter UI state.
    this.accept({ type: "connection_pending", request_id: command.request_id, route_id: command.route_id });
    try { await this.sendCommand(command); }
    catch (error) {
      this.accept({ type: "connection_result", request_id: command.request_id, error: error instanceof Error ? error.message : String(error) });
      throw error;
    }
  }
  private async sendCommand(command: UIEvent): Promise<void> {
    if (this.closing || this.state.status === "disconnected") throw new Error("Backend disconnected; request was not sent");
    if (this.state.status !== "ready") await new Promise<void>((resolve, reject) => {
      const entry = { resolve: () => { clearTimeout(timer); resolve(); }, reject: (e: Error) => { clearTimeout(timer); reject(e); } };
      const timer = setTimeout(() => { this.waiters.delete(entry); reject(new Error("Backend not ready; request was not sent")); }, 30000);
      this.waiters.add(entry);
    });
    if (this.closing || this.state.status === "disconnected") throw new Error("Backend disconnected; request was not sent");
    if (command.type === "select_model") {
      if (this.modelSelection) throw new Error("模型正在切换，请稍候。");
      return new Promise<void>((resolve, reject) => {
        const pending = { request_id: command.request_id, resolve, reject, timer: setTimeout(() => {
          this.modelSelection = undefined;
          reject(new Error("模型切换尚未确认，请查看当前模型后再试。"));
        }, 15000) };
        this.modelSelection = pending;
        try { this.write({ type: "command", cmd: `/model ${command.model_key}`, request_id: command.request_id }); }
        catch (error) { clearTimeout(pending.timer); this.modelSelection = undefined; reject(error); }
      });
    }
    if (!["message", "image"].includes(command.type)) { this.write(command); return; }
    const sid = command.submission_id;
    if (!sid || this.submissions.has(sid)) throw new Error("Invalid or pending submission ID");
    return new Promise<void>((resolve, reject) => {
      const pending = { resolve, reject, timer: setTimeout(() => {
        try { this.write({ type: "submission_status", submission_id: sid }); } catch { /* unknown below */ }
        pending.timer = setTimeout(() => {
          this.submissions.delete(sid);
          this.accept({ type: "submission_status", submission_id: sid, status: "unknown" });
          reject(new Error("请求接收状态未知，草稿已保留。请先查看结果，勿重复发送。"));
        }, 5000);
      }, 20000) };
      this.submissions.set(sid, pending);
      this.accept({ type: "gui_user", text: command.type === "message" ? command.text : `${command.prompt}\n\n📎 ${command.path}`, submission_id: sid });
      try { this.write(command); }
      catch (error) { clearTimeout(pending.timer); this.submissions.delete(sid); reject(error); }
    });
  }
  write(command: UIEvent) {
    if (!this.child || this.child.stdin.destroyed || this.child.stdin.writableEnded) throw new Error("Backend disconnected");
    if (this.child.stdin.writableLength > 4 * 1024 * 1024) throw new Error("Backend input is busy; request was not sent");
    this.child.stdin.write(JSON.stringify(command) + "\n");
  }
  query(method: string, params: Record<string, unknown>): Promise<any> {
    const request_id = randomUUID();
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => { this.queries.delete(request_id); reject(new Error("Query timed out")); }, 15000);
      this.queries.set(request_id, { resolve, reject, timer });
      try { this.write({ type: "ui_query", method, params, request_id }); }
      catch (error) { clearTimeout(timer); this.queries.delete(request_id); reject(error); }
    });
  }
  async close(): Promise<void> {
    if (this.closePromise) return this.closePromise;
    this.closing = true;
    const child = this.child;
    if (!child) return;
    this.closePromise = new Promise<void>(resolve => {
      const force = setTimeout(() => child.kill("SIGTERM"), 15000);
      const timeout = setTimeout(() => { child.kill("SIGKILL"); resolve(); }, 20000);
      child.once("close", () => { clearTimeout(force); clearTimeout(timeout); resolve(); });
      try { this.write({ type: "exit" }); } catch { child.kill(); }
    });
    return this.closePromise;
  }
}
