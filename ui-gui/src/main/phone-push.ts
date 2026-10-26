import { checkedSubscription, type PhoneDevices } from "./phone-devices.js";
import type { PushSender, PushSubscription, VapidKey } from "./web-push.js";

/** What the desktop would show as a notification, and whether it is waiting for an answer. */
export type PushNotice = { title: string; body: string; runtime?: string; urgent?: boolean };
type Target = { id: string; push: PushSubscription };

/**
 * Notifications for paired phones that do not have the app open. An approval or a question goes at once;
 * anything else waits up to a minute and goes as one notification with whatever else came meanwhile, so a
 * busy hour does not ring the phone over and over. A subscription the push service no longer knows is dropped.
 */
export class PhonePush {
  private batches = new Map<string, PushNotice[]>();
  private timers = new Map<string, ReturnType<typeof setTimeout>>();

  constructor(private readonly options: {
    targets: () => Target[]; live: (deviceId: string) => boolean; send: PushSender; drop: (deviceId: string) => void;
    windowMs?: number; report?: (error: unknown) => void;
  }) {}

  notify(notice: PushNotice) {
    for (const target of this.options.targets()) {
      if (this.options.live(target.id)) continue;
      if (notice.urgent) { void this.deliver(target, [notice], true); continue; }
      const batch = this.batches.get(target.id) ?? [];
      batch.push(notice);
      this.batches.set(target.id, batch);
      if (!this.timers.has(target.id)) {
        this.timers.set(target.id, setTimeout(() => { void this.flush(target.id); }, this.options.windowMs ?? 60_000));
      }
    }
  }

  async flush(deviceId: string) {
    clearTimeout(this.timers.get(deviceId));
    this.timers.delete(deviceId);
    const batch = this.batches.get(deviceId) ?? [];
    this.batches.delete(deviceId);
    const target = this.options.targets().find(item => item.id === deviceId);
    if (!target || !batch.length || this.options.live(deviceId)) return;
    await this.deliver(target, batch, false);
  }

  dispose() { for (const timer of this.timers.values()) clearTimeout(timer); this.timers.clear(); this.batches.clear(); }

  private async deliver(target: Target, notices: PushNotice[], urgent: boolean) {
    const last = notices[notices.length - 1];
    const payload = notices.length === 1
      ? { title: last.title, body: last.body, runtime: last.runtime ?? "" }
      : { title: `Astra：${notices.length} 条新消息`, body: notices.map(notice => notice.title).join("；").slice(0, 300), runtime: last.runtime ?? "" };
    try {
      const status = await this.options.send(target.push, payload, { urgent });
      if (status === 404 || status === 410) this.options.drop(target.id);
    } catch (error) { this.options.report?.(error); }
  }
}

/** A phone turning its notifications on or off: the key it subscribes with, then its subscription. */
export function phonePushRequest(devices: PhoneDevices, key: () => VapidKey, deviceId: string, action: unknown, subscription?: unknown) {
  if (action === "key") return { publicKey: key().publicKey };
  if (action === "status") return { enabled: devices.pushTargets().some(target => target.id === deviceId) };
  if (action !== "subscribe" && action !== "unsubscribe") throw new Error("Unsupported push call");
  devices.setPush(deviceId, action === "subscribe" ? checkedSubscription(subscription) : undefined);
  return { enabled: action === "subscribe" };
}
