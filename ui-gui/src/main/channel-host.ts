import { readFileSync } from "node:fs";

/** The part of a session backend the host needs: whether it still runs, and a way to close it. */
export type HostedBackend = { state: { status: string }; close(): Promise<void> };

const TRUE = new Set(["1", "true", "yes", "on"]);

/** Whether channels.json switches any messaging channel on (the same reading as the backend's config). */
export function channelsEnabled(configPath: string): boolean {
  let data: unknown;
  try { data = JSON.parse(readFileSync(configPath, "utf8")); } catch { return false; }
  if (!data || typeof data !== "object") return false;
  const sections = data as Record<string, unknown>;
  return ["qq", "onebot", "weixin"].some(name => {
    const section = sections[name];
    const value = section && typeof section === "object" ? (section as Record<string, unknown>).enabled : undefined;
    return value === true || (typeof value === "string" && TRUE.has(value.trim().toLowerCase()));
  });
}

/**
 * Keeps one backend running for the messaging channels while the desktop is open, so WeChat and QQ
 * answer even when no conversation is open in the window. It is not a session the window shows: it
 * holds no conversation of its own, and each chat keeps its own conversation as before. It follows
 * channels.json (started when a channel is switched on, closed when none is) and is started again
 * after it exits, at most once per ``retryMs``.
 */
export class ChannelHost {
  private backend?: HostedBackend;
  private timer?: ReturnType<typeof setInterval>;
  private startedAt = -Infinity;
  private checking = false;
  private stopped = false;

  constructor(private readonly options: {
    configPath: string; create: () => HostedBackend; intervalMs?: number; retryMs?: number; now?: () => number;
  }) {}

  get running() { return !!this.backend && this.backend.state.status !== "disconnected"; }

  start() {
    void this.check();
    this.timer = setInterval(() => { void this.check(); }, this.options.intervalMs ?? 10_000);
  }

  async check() {
    if (this.stopped || this.checking) return;
    this.checking = true;
    try {
      const wanted = channelsEnabled(this.options.configPath);
      const now = (this.options.now ?? Date.now)();
      if (wanted && !this.running && now - this.startedAt >= (this.options.retryMs ?? 30_000)) {
        this.startedAt = now;
        this.backend = this.options.create();
      } else if (!wanted && this.backend) {
        const backend = this.backend;
        this.backend = undefined;
        await backend.close();
      }
    } finally { this.checking = false; }
  }

  async stop() {
    this.stopped = true;
    clearInterval(this.timer);
    const backend = this.backend;
    this.backend = undefined;
    await backend?.close();
  }
}
