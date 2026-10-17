import { phoneAuthMessage, type PhoneStreamEvent } from "../phone-protocol.js";
import { parseEventStream } from "./state.js";

/** What this phone keeps: its id on the desktop and its signing key, which can sign but never be read out. */
export type PhoneDeviceRecord = { deviceId: string; name: string; privateKey: CryptoKey };
export type DeviceStorage = {
  load(): Promise<PhoneDeviceRecord | undefined>;
  save(record: PhoneDeviceRecord): Promise<void>;
  clear(): Promise<void>;
};

/** The desktop refused this phone: it was removed there, or never paired. Pairing again is the way back. */
export class PhoneUnpairedError extends Error {}

export class PhoneClient {
  // Safari's fetch must be called on the window: stored and called as this.fetcher(...) it throws
  // "Can only call Window.fetch on instances of Window", so the default calls it as a plain function.
  constructor(private readonly storage: DeviceStorage, private readonly base = "",
              private readonly fetcher: typeof fetch = (input, init) => fetch(input, init)) {}

  device() { return this.storage.load(); }

  async pair(code: string, name: string): Promise<PhoneDeviceRecord> {
    const keys = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
    const publicKey = await crypto.subtle.exportKey("jwk", keys.publicKey);
    const result = await this.post("/api/pair", { code, name, publicKey });
    const record = { deviceId: String(result.deviceId), name: String(result.name), privateKey: keys.privateKey };
    await this.storage.save(record);
    return record;
  }

  async forget() { await this.storage.clear(); }

  /** Proves this phone holds its key and returns a bearer token for the event stream. */
  async signIn(record: PhoneDeviceRecord): Promise<string> {
    let nonce: string;
    try { nonce = String((await this.post("/api/challenge", { deviceId: record.deviceId })).nonce); }
    catch (error) { if (error instanceof RefusedError) throw new PhoneUnpairedError(error.message); throw error; }
    const signature = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, record.privateKey,
      new TextEncoder().encode(phoneAuthMessage(nonce, record.deviceId)));
    const session = await this.post("/api/session", { deviceId: record.deviceId, nonce, signature: base64url(signature) });
    return String(session.token);
  }

  /** Follows the desktop until the stream ends or the signal aborts. A 401 means the token must be renewed. */
  async follow(token: string, onMessage: (message: PhoneStreamEvent) => void, signal: AbortSignal): Promise<void> {
    const response = await this.fetcher(`${this.base}/api/events`, { headers: { Authorization: `Bearer ${token}` }, signal, cache: "no-store" });
    if (response.status === 401) throw new TokenExpiredError();
    if (!response.ok || !response.body) throw new Error(`连接失败（${response.status}）`);
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) return;
      const parsed = parseEventStream(buffer + value);
      buffer = parsed.rest;
      for (const payload of parsed.payloads) onMessage(JSON.parse(payload) as PhoneStreamEvent);
    }
  }

  private async post(path: string, body: unknown): Promise<Record<string, unknown>> {
    const response = await this.fetcher(`${this.base}${path}`, { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body), cache: "no-store" });
    const result = await response.json().catch(() => ({})) as Record<string, unknown>;
    if (response.status === 403) throw new RefusedError(String(result.error || "被拒绝"));
    if (!response.ok) throw new Error(String(result.error || `请求失败（${response.status}）`));
    return result;
  }
}

export class TokenExpiredError extends Error {}
class RefusedError extends Error {}

function base64url(buffer: ArrayBuffer): string {
  let text = "";
  for (const byte of new Uint8Array(buffer)) text += String.fromCharCode(byte);
  return btoa(text).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

/** The browser's storage: a CryptoKey is stored as itself in IndexedDB, so the private key never exists as bytes. */
export function indexedDbStorage(): DeviceStorage {
  const open = () => new Promise<IDBDatabase>((resolve, reject) => {
    const request = indexedDB.open("astra-phone", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("device");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
  const run = async <T>(mode: IDBTransactionMode, action: (store: IDBObjectStore) => IDBRequest<T>) => {
    const db = await open();
    try {
      return await new Promise<T>((resolve, reject) => {
        const request = action(db.transaction("device", mode).objectStore("device"));
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
    } finally { db.close(); }
  };
  return {
    load: () => run<PhoneDeviceRecord | undefined>("readonly", store => store.get("current")),
    save: async record => { await run("readwrite", store => store.put(record, "current")); },
    clear: async () => { await run("readwrite", store => store.delete("current")); },
  };
}
