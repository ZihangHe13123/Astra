import { createPublicKey, randomBytes, randomInt, randomUUID, timingSafeEqual, verify, type KeyObject } from "node:crypto";
import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";
import { PAIRING_ALPHABET, PAIRING_CODE_LENGTH, phoneAuthMessage, type PhoneDevice } from "../phone-protocol.js";

const PAIRING_SECONDS = 300;
const PAIRING_ATTEMPTS = 5;
const CHALLENGE_SECONDS = 60;
const TOKEN_SECONDS = 12 * 3600;

/** A public key as WebCrypto exports it; a private part ("d") is never accepted. */
type JsonWebKey = { kty?: string; crv?: string; x?: string; y?: string; d?: string };
type StoredDevice = PhoneDevice & { publicKey: JsonWebKey };
type Stored = { enabled: boolean; devices: StoredDevice[] };

/**
 * Phones paired with this desktop. Each holds its own non-extractable P-256 key and proves it by
 * signing a one-time challenge; there is no shared password. A pairing code is shown only in the
 * window, lives five minutes, works once, and is withdrawn after five wrong tries.
 */
export class PhoneDevices {
  private stored: Stored = { enabled: false, devices: [] };
  private keys = new Map<string, KeyObject>();
  private pairing?: { code: string; expiresAt: number; failures: number };
  private challenges = new Map<string, { deviceId: string; expiresAt: number }>();
  private tokens = new Map<string, { deviceId: string; expiresAt: number }>();
  private revokedListeners = new Set<(deviceId: string) => void>();

  constructor(private readonly path: string, private readonly now: () => number = Date.now) {
    try {
      const data = JSON.parse(readFileSync(path, "utf8"));
      const devices = Array.isArray(data?.devices) ? data.devices : [];
      this.stored = { enabled: data?.enabled === true, devices: [] };
      for (const device of devices) {
        try { this.keys.set(device.id, publicKey(device.publicKey)); this.stored.devices.push(device); }
        catch { /* an entry that no longer reads is dropped, not trusted */ }
      }
    } catch { /* first use */ }
  }

  get enabled() { return this.stored.enabled; }
  setEnabled(enabled: boolean) { this.stored.enabled = enabled; this.save(); if (!enabled) this.endPairing(); }

  list(): PhoneDevice[] {
    return this.stored.devices.map(({ id, name, createdAt, lastSeen }) => ({ id, name, createdAt, ...(lastSeen ? { lastSeen } : {}) }));
  }

  currentPairing(): { code: string; expiresAt: number } | undefined {
    if (this.pairing && this.pairing.expiresAt > this.now()) return { code: this.pairing.code, expiresAt: this.pairing.expiresAt };
    this.pairing = undefined;
    return undefined;
  }

  startPairing(): { code: string; expiresAt: number } {
    const code = Array.from({ length: PAIRING_CODE_LENGTH }, () => PAIRING_ALPHABET[randomInt(PAIRING_ALPHABET.length)]).join("");
    this.pairing = { code, expiresAt: this.now() + PAIRING_SECONDS * 1000, failures: 0 };
    return { code, expiresAt: this.pairing.expiresAt };
  }

  endPairing() { this.pairing = undefined; }

  /** Registers the phone's public key under a fresh id. Throws when the code is wrong, used or old. */
  pair(code: unknown, name: unknown, key: unknown): PhoneDevice {
    const pairing = this.currentPairing() && this.pairing;
    if (!pairing) throw new PhoneAuthError("没有进行中的配对，请在电脑上的设置里点“添加手机”。");
    const given = Buffer.from(String(code ?? "").toUpperCase().replace(/[\s-]/g, ""));
    const expected = Buffer.from(pairing.code);
    if (given.length !== expected.length || !timingSafeEqual(given, expected)) {
      pairing.failures += 1;
      if (pairing.failures >= PAIRING_ATTEMPTS) this.pairing = undefined;
      throw new PhoneAuthError("配对码不对。");
    }
    const keyObject = publicKey(key);
    const device: StoredDevice = { id: randomUUID(), name: String(name ?? "").trim().slice(0, 60) || "iPhone",
      createdAt: new Date(this.now()).toISOString(), publicKey: keyObject.export({ format: "jwk" }) as JsonWebKey };
    this.pairing = undefined;
    this.stored.devices.push(device); this.keys.set(device.id, keyObject); this.save();
    const { publicKey: _key, ...shown } = device;
    return shown;
  }

  revoke(deviceId: string): boolean {
    const before = this.stored.devices.length;
    this.stored.devices = this.stored.devices.filter(device => device.id !== deviceId);
    if (this.stored.devices.length === before) return false;
    this.keys.delete(deviceId);
    for (const [token, entry] of this.tokens) if (entry.deviceId === deviceId) this.tokens.delete(token);
    for (const [nonce, entry] of this.challenges) if (entry.deviceId === deviceId) this.challenges.delete(nonce);
    this.save();
    for (const listener of this.revokedListeners) listener(deviceId);
    return true;
  }

  onRevoked(listener: (deviceId: string) => void) { this.revokedListeners.add(listener); return () => this.revokedListeners.delete(listener); }

  challenge(deviceId: unknown): string {
    const id = String(deviceId ?? "");
    if (!this.keys.has(id)) throw new PhoneAuthError("这台手机没有配对，或已在电脑上被移除。");
    this.expire();
    const nonce = randomBytes(32).toString("base64url");
    this.challenges.set(nonce, { deviceId: id, expiresAt: this.now() + CHALLENGE_SECONDS * 1000 });
    return nonce;
  }

  /** Checks the signed challenge (WebCrypto ECDSA P-256 / SHA-256, raw r||s) and issues a session token. */
  signIn(deviceId: unknown, nonce: unknown, signature: unknown): { token: string; expiresAt: number } {
    const id = String(deviceId ?? ""), challenge = String(nonce ?? "");
    const entry = this.challenges.get(challenge);
    this.challenges.delete(challenge);
    const key = this.keys.get(id);
    if (!entry || entry.deviceId !== id || entry.expiresAt <= this.now() || !key) throw new PhoneAuthError("登录挑战已失效，请重试。");
    let valid = false;
    try {
      valid = verify("sha256", Buffer.from(phoneAuthMessage(challenge, id)), { key, dsaEncoding: "ieee-p1363" },
        Buffer.from(String(signature ?? ""), "base64url"));
    } catch { valid = false; }
    if (!valid) throw new PhoneAuthError("签名不对。");
    this.expire();
    const token = randomBytes(32).toString("base64url");
    const expiresAt = this.now() + TOKEN_SECONDS * 1000;
    this.tokens.set(token, { deviceId: id, expiresAt });
    const device = this.stored.devices.find(item => item.id === id);
    if (device) { device.lastSeen = new Date(this.now()).toISOString(); this.save(); }
    return { token, expiresAt };
  }

  /** The device a bearer token belongs to, while it is valid and the device is still paired. */
  deviceForToken(token: string): string | undefined {
    const entry = this.tokens.get(token);
    if (!entry || entry.expiresAt <= this.now() || !this.keys.has(entry.deviceId)) { this.tokens.delete(token); return undefined; }
    return entry.deviceId;
  }

  private expire() {
    const now = this.now();
    for (const [nonce, entry] of this.challenges) if (entry.expiresAt <= now) this.challenges.delete(nonce);
    for (const [token, entry] of this.tokens) if (entry.expiresAt <= now) this.tokens.delete(token);
  }

  private save() {
    mkdirSync(dirname(this.path), { recursive: true, mode: 0o700 });
    const temporary = `${this.path}.${process.pid}.tmp`;
    writeFileSync(temporary, JSON.stringify(this.stored, null, 2), { mode: 0o600 });
    renameSync(temporary, this.path);
  }
}

export class PhoneAuthError extends Error {}

/** Only an EC P-256 public key is accepted; anything else (or a private key) is refused. */
function publicKey(value: unknown): KeyObject {
  const jwk = value as JsonWebKey | undefined;
  if (!jwk || typeof jwk !== "object" || jwk.kty !== "EC" || jwk.crv !== "P-256" || typeof jwk.x !== "string"
      || typeof jwk.y !== "string" || "d" in jwk) throw new PhoneAuthError("设备公钥格式不对。");
  return createPublicKey({ key: { kty: "EC", crv: "P-256", x: jwk.x, y: jwk.y }, format: "jwk" });
}
