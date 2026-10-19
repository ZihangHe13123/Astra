import type { Bootstrap, DesktopBridge, DesktopEvent, Preferences } from "../bridge.js";
import { SHARED_PREFERENCE_KEYS, type PhoneInvokeMethod, type PhoneStreamControl, type PhoneStreamPosition } from "../phone-protocol.js";
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
};

const DEFAULTS: Preferences = { theme: "system", drafts: {}, attachments: {}, workspaces: {}, pinned: [], projects: [], titles: {}, timeline: true };
const UNAVAILABLE = {
  choose: "手机上还不能选择文件或文件夹。",
  file: "文件预览和打开只在电脑上可用。",
  appshot: "窗口捕获只在电脑上可用。",
  export: "导出会话只在电脑上可用。",
  phone: "手机配对只能在电脑上管理。",
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
      }
      if (this.stopped) return;
      await this.env.sleep(delay);
      delay = Math.min(delay * 2, 10_000);
    }
  }

  /** Stops following the desktop (the page is going away). */
  dispose() { this.stopped = true; this.stream?.abort(); }

  private receive(data: string, id?: number) {
    const message = JSON.parse(data) as DesktopEvent | PhoneStreamControl;
    if ("reload" in message) { this.env.reload(); return; }
    if ("ready" in message) {
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

  /** After the phone wakes, a stream iOS suspended is replaced at once instead of waiting to time out. */
  wake() { if (this.streaming) this.stream?.abort(); }

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
    return { ...boot, preferences: this.merged() };
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
  sessionAction(action: "export" | "delete", entry: { name: string; mode: string }) {
    return action === "delete" ? this.call("deleteSession", [entry]) : Promise.reject(new Error(UNAVAILABLE.export));
  }
  async preferences(value: Partial<Preferences>) { this.store(value); return this.merged(); }
  flushPreferences(value: Partial<Preferences>) { this.store(value); }
  choose(): Promise<string[]> { return Promise.reject(new Error(UNAVAILABLE.choose)); }
  clipboardImage() { return Promise.resolve(null); }
  copyText(text: string) { return this.env.copy(text); }
  appshot(): Promise<void> { return Promise.reject(new Error(UNAVAILABLE.appshot)); }
  droppedPaths() { return []; }
  async openExternal(url: string) { if (/^https?:\/\//i.test(url)) this.env.open(url); }
  file(): Promise<never> { return Promise.reject(new Error(UNAVAILABLE.file)); }
  cancelPreview() { return Promise.resolve(); }
  phone(): Promise<never> { return Promise.reject(new Error(UNAVAILABLE.phone)); }
}
