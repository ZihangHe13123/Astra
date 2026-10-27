import type { Bootstrap, DesktopBridge, DesktopEvent, Preferences } from "../bridge.js";
import { SHARED_PREFERENCE_KEYS, type PhoneInvokeMethod, type PhoneNotificationState, type PhoneStreamControl, type PhoneStreamPosition } from "../phone-protocol.js";
import { PhoneUnpairedError, TokenExpiredError, type PhoneClient, type PhoneDeviceRecord } from "./client.js";

/** What the bridge needs from the phone's browser; tests give it their own. */
export type BridgeEnvironment = {
  client: PhoneClient;
  /** The phone's own preferences (theme, drafts, attachments): kept on the phone, never sent. */
  load(): string | null;
  save(text: string): void;
  /** Shows the pairing screen and resolves once this phone is paired. */
  pair(notice?: string): Promise<PhoneDeviceRecord>;
  /** The stream missed more than the desktop kept: the page starts again from a fresh snapshot. */
  reload(): void;
  copy(text: string): Promise<void>;
  open(url: string): void;
  sleep(ms: number): Promise<void>;
  /** Speech the desktop routed to this phone (a voice_audio event): play it here, fetching the audio with `load`. */
  voice?(event: { id?: string; seconds?: number; at?: number; stop?: boolean }, load: (id: string) => Promise<ArrayBuffer>): void;
  /** Web Push in this browser: why it cannot work here (""), asking permission, subscribing with the desktop's key. */
  push?: {
    unsupported(): string;
    permission(): Promise<NotificationPermission | string>;
    subscribe(publicKey: string): Promise<unknown>;
    unsubscribe(): Promise<void>;
  };
  /** The session a notification opened the app for, if any. */
  opening?: string;
  /** The phone's own picker: camera, photo library or files. Resolves with nothing when cancelled. */
  pickFiles?(): Promise<File[]>;
};

const DEFAULTS: Preferences = { theme: "system", drafts: {}, attachments: {}, workspaces: {}, pinned: [], projects: [], titles: {}, timeline: true };
const UNAVAILABLE = {
  choose: "手机上不能选择文件夹。",
  file: "文件预览和打开只在电脑上可用。",
  appshot: "窗口捕获只在电脑上可用。",
  export: "导出会话只在电脑上可用。",
  phone: "手机配对只能在电脑上管理。",
  data: "导出和清除数据只能在电脑上操作。",
  approvalRules: "审批规则只能在电脑上管理。",
  usage: "预算和单价只能在电脑上设置。",
};

/**
 * The desktop renderer's `window.astra` on a paired phone. Calls about sessions go to the desktop;
 * events arrive on one stream that resumes where it stopped. Mac-only actions say so instead.
 */
export class RemoteBridge implements DesktopBridge {
  readonly remote = true;
  private device?: PhoneDeviceRecord;
  private token?: string;
  private signingIn?: Promise<string>;
  private position?: PhoneStreamPosition;
  private handlers = new Set<(events: DesktopEvent[]) => void>();
  private queue: DesktopEvent[] = [];
  private streaming = false;
  private stream?: AbortController;
  /** While the page is hidden the stream stays closed, so the desktop knows to notify instead. */
  private hidden = false;
  private shown?: () => void;
  /** Failed attempts to reach the Mac in a row, and whether the page was told it is unreachable. */
  private failures = 0;
  private offline = false;
  private shared: Partial<Preferences> = {};
  private readyWaiters: (() => void)[] = [];
  private isReady = false;
  private stopped = false;

  constructor(private readonly env: BridgeEnvironment) {}

  /** A valid token, pairing first if this phone has no key or was removed on the desktop. */
  private authorize(): Promise<string> {
    if (this.token) return Promise.resolve(this.token);
    this.signingIn ??= (async () => {
      this.device ??= await this.env.client.device() ?? await this.env.pair();
      try { this.token = await this.env.client.signIn(this.device); }
      catch (error) {
        if (!(error instanceof PhoneUnpairedError)) throw error;
        await this.env.client.forget();
        this.device = await this.env.pair("这台手机已在电脑上移除，请重新配对。");
        this.token = await this.env.client.signIn(this.device);
      }
      return this.token;
    })().finally(() => { this.signingIn = undefined; });
    return this.signingIn;
  }

  private async call(method: PhoneInvokeMethod, args: unknown[]): Promise<any> {
    try { return await this.env.client.invoke(await this.authorize(), method, args); }
    catch (error) {
      if (!(error instanceof TokenExpiredError)) throw error;
      this.token = undefined;
      return this.env.client.invoke(await this.authorize(), method, args);
    }
  }

  private start() {
    if (this.streaming) return;
    this.streaming = true;
    void this.follow();
  }

  private async follow() {
    let delay = 1000;
    while (!this.stopped) {
      this.stream = new AbortController();
      try {
        await this.env.client.follow(await this.authorize(), this.position, (data, id) => { this.receive(data, id); delay = 1000; }, this.stream.signal);
      } catch (error) {
        if (error instanceof TokenExpiredError) this.token = undefined;
        // Two misses in a row (not a stream closed on purpose) say the Mac cannot be reached right now.
        else if (!this.stream?.signal.aborted && !this.stopped && ++this.failures >= 2 && !this.offline) {
          this.offline = true; this.deliver([{ link: "offline" }]);
        }
      }
      if (this.stopped) return;
      if (this.hidden) { await new Promise<void>(resolve => { this.shown = resolve; }); this.shown = undefined; continue; }
      await this.env.sleep(delay);
      delay = Math.min(delay * 2, 10_000);
    }
  }

  /** Stops following the desktop (the page is going away). */
  dispose() { this.stopped = true; this.stream?.abort(); this.shown?.(); }

  private deliver(events: DesktopEvent[]) { for (const handler of this.handlers) handler(events); }

  private receive(data: string, id?: number) {
    const message = JSON.parse(data) as DesktopEvent | PhoneStreamControl;
    if ("reload" in message) { this.env.reload(); return; }
    if ("ready" in message) {
      this.failures = 0;
      if (this.offline) { this.offline = false; this.deliver([{ link: "online" }]); }
      this.position ??= { seq: message.seq, instance: message.instance };
      this.isReady = true;
      for (const resolve of this.readyWaiters.splice(0)) resolve();
      return;
    }
    if (id !== undefined && this.position) this.position.seq = id;
    if ("event" in message && message.event?.type === "voice_audio") {
      this.env.voice?.(message.event as { id?: string; at?: number; stop?: boolean }, voice => this.loadVoice(voice));
    }
    this.queue.push(message);
    if (this.queue.length === 1) queueMicrotask(() => {
      const events = this.queue; this.queue = [];
      for (const handler of this.handlers) handler(events);
    });
  }

  private async loadVoice(id: string): Promise<ArrayBuffer> {
    try { return await this.env.client.voice(await this.authorize(), id); }
    catch (error) {
      if (!(error instanceof TokenExpiredError)) throw error;
      this.token = undefined;
      return this.env.client.voice(await this.authorize(), id);
    }
  }

  /**
   * The page is hidden: the stream closes now, while iOS still lets it, rather than lingering half-open behind
   * Tailscale; the desktop then sees no phone watching and sends notifications. What happens meanwhile is
   * replayed from the same position on wake.
   */
  hide() { if (!this.streaming || this.hidden) return; this.hidden = true; this.stream?.abort(); }

  /** After the phone wakes, a stream iOS suspended is replaced at once instead of waiting to time out. */
  wake() {
    if (!this.streaming) return;
    if (this.hidden) { this.hidden = false; this.shown?.(); return; }
    this.stream?.abort();
  }

  private ready(): Promise<void> {
    this.start();
    return this.isReady ? Promise.resolve() : new Promise(resolve => this.readyWaiters.push(resolve));
  }

  private local(): Partial<Preferences> {
    try { return JSON.parse(this.env.load() || "{}"); } catch { return {}; }
  }

  private merged(): Preferences { return { ...DEFAULTS, ...this.local(), ...this.shared }; }

  private store(value: Partial<Preferences>) {
    const own = Object.fromEntries(Object.entries({ ...this.local(), ...value })
      .filter(([key]) => !(SHARED_PREFERENCE_KEYS as readonly string[]).includes(key)));
    this.env.save(JSON.stringify(own));
  }

  async bootstrap(): Promise<Bootstrap> {
    // The snapshot is taken after the stream is open, so no event falls between the two.
    await this.ready();
    const boot = await this.call("bootstrap", []) as Bootstrap;
    this.shared = boot.preferences ?? {};
    // Opened from a notification: straight to that conversation, while it is still open on the desktop.
    const opening = this.env.opening && boot.sessions.some(session => session.id === this.env.opening) ? this.env.opening : "";
    return { ...boot, ...(opening ? { active: opening } : {}), preferences: this.merged() };
  }

  onEvents(handler: (events: DesktopEvent[]) => void) {
    this.handlers.add(handler);
    this.start();
    return () => { this.handlers.delete(handler); };
  }

  create(options: { session?: string; mode?: string; workspace?: string }) { return this.call("create", [options]); }
  select() { return Promise.resolve(); }
  close(id: string) { return this.call("close", [id]); }
  send(id: string, command: Record<string, unknown>) { return this.call("send", [id, command]); }
  query(method: string, params?: Record<string, unknown>, runtime?: string) { return this.call("query", [method, params ?? {}, runtime]); }
  jobs(action: string, value?: string, extra?: string) { return this.call("jobs", [action, value, extra]); }
  memory(action: string, ...values: string[]) { return this.call("memory", [action, ...values]) as Promise<string>; }
  data(): Promise<never> { return Promise.reject(new Error(UNAVAILABLE.data)); }
  approvalRules(): Promise<never> { return Promise.reject(new Error(UNAVAILABLE.approvalRules)); }
  usage(): Promise<never> { return Promise.reject(new Error(UNAVAILABLE.usage)); }
  sessionAction(action: "export" | "delete", entry: { name: string; mode: string }) {
    return action === "delete" ? this.call("deleteSession", [entry]) : Promise.reject(new Error(UNAVAILABLE.export));
  }
  async preferences(value: Partial<Preferences>) { this.store(value); return this.merged(); }
  flushPreferences(value: Partial<Preferences>) { this.store(value); }
  /** Files only: opened straight from the tap, as iOS shows its picker only then; each is then sent to the Mac. */
  choose(kind: "files" | "folder" = "files"): Promise<string[]> {
    if (kind !== "files") return Promise.reject(new Error(UNAVAILABLE.choose));
    if (!this.env.pickFiles) return Promise.reject(new Error("这个浏览器不能选择文件。"));
    return this.env.pickFiles().then(files => this.upload(files));
  }

  async upload(files: File[]): Promise<string[]> {
    const paths: string[] = [];
    for (const file of files) {
      const name = file.name || `${file.type.startsWith("image/") ? "photo" : "file"}.${file.type.split("/")[1] || "bin"}`;
      try { paths.push(await this.env.client.upload(await this.authorize(), file, name)); }
      catch (error) {
        if (!(error instanceof TokenExpiredError)) throw error;
        this.token = undefined;
        paths.push(await this.env.client.upload(await this.authorize(), file, name));
      }
    }
    return paths;
  }
  clipboardImage() { return Promise.resolve(null); }
  copyText(text: string) { return this.env.copy(text); }
  appshot(): Promise<void> { return Promise.reject(new Error(UNAVAILABLE.appshot)); }
  droppedPaths() { return []; }
  async openExternal(url: string) { if (/^https?:\/\//i.test(url)) this.env.open(url); }
  file(): Promise<never> { return Promise.reject(new Error(UNAVAILABLE.file)); }
  cancelPreview() { return Promise.resolve(); }
  phone(): Promise<never> { return Promise.reject(new Error(UNAVAILABLE.phone)); }

  async notifications(action: "status" | "enable" | "disable"): Promise<PhoneNotificationState> {
    const push = this.env.push;
    const reason = push ? push.unsupported() : "这个浏览器不支持通知。";
    if (!push || reason) return { state: "unsupported", reason };
    if (action === "disable") {
      await push.unsubscribe();
      await this.call("push", ["unsubscribe"]);
      return { state: "off" };
    }
    if (action === "status") return { state: (await this.call("push", ["status"])).enabled ? "on" : "off" };
    // iOS asks only from a tap, so this runs straight from the button.
    if (await push.permission() !== "granted") return { state: "denied", reason: "通知被关掉了：到 iPhone 的“设置 → 通知 → Astra”打开。" };
    const { publicKey } = await this.call("push", ["key"]) as { publicKey: string };
    await this.call("push", ["subscribe", await push.subscribe(publicKey)]);
    return { state: "on" };
  }
}
