import { createCipheriv, createECDH, createPrivateKey, generateKeyPairSync, hkdfSync, randomBytes, sign, type ECDH, type KeyObject } from "node:crypto";
import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

/**
 * Standard Web Push from the desktop to a paired phone: the payload is encrypted for that phone's
 * subscription (RFC 8291, aes128gcm) and the request is signed with this desktop's VAPID key (RFC 8292),
 * so the push service (Apple's, for an iPhone) carries only ciphertext. Node's crypto does all of it.
 */

export type PushSubscription = { endpoint: string; keys: { p256dh: string; auth: string } };
type Jwk = { kty: "EC"; crv: "P-256"; x: string; y: string; d: string };

const b64url = (data: Buffer) => data.toString("base64url");
const unb64url = (text: string) => Buffer.from(text, "base64url");
const RECORD_SIZE = 4096;

/** The desktop's VAPID key pair: made once, kept beside the paired devices, readable only by this user. */
export class VapidKey {
  private readonly key: KeyObject;
  private readonly jwk: Jwk;

  constructor(path: string) {
    let jwk: Jwk | undefined;
    try {
      const stored = JSON.parse(readFileSync(path, "utf8"));
      if (stored?.kty === "EC" && stored.crv === "P-256" && stored.d) jwk = stored;
    } catch { /* first use */ }
    if (!jwk) {
      jwk = generateKeyPairSync("ec", { namedCurve: "prime256v1" }).privateKey.export({ format: "jwk" }) as Jwk;
      mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
      const temporary = `${path}.${process.pid}.tmp`;
      writeFileSync(temporary, JSON.stringify(jwk), { mode: 0o600 });
      renameSync(temporary, path);
    }
    this.jwk = jwk;
    this.key = createPrivateKey({ key: jwk, format: "jwk" });
  }

  /** The uncompressed public point, as a page passes it to pushManager.subscribe. */
  get publicKey(): string {
    return b64url(Buffer.concat([Buffer.from([4]), unb64url(this.jwk.x), unb64url(this.jwk.y)]));
  }

  /** The Authorization header for one push service: a short-lived ES256 JWT for its origin. */
  authorization(endpoint: string, subject: string, now = Date.now()): string {
    const header = b64url(Buffer.from(JSON.stringify({ typ: "JWT", alg: "ES256" })));
    const claims = b64url(Buffer.from(JSON.stringify({
      aud: new URL(endpoint).origin, exp: Math.floor(now / 1000) + 12 * 3600, sub: subject,
    })));
    const signature = sign("sha256", Buffer.from(`${header}.${claims}`), { key: this.key, dsaEncoding: "ieee-p1363" });
    return `vapid t=${header}.${claims}.${b64url(signature)}, k=${this.publicKey}`;
  }
}

/** The body of one push: the payload encrypted for the subscription, with the record header in front. */
export function encryptPayload(subscription: PushSubscription, payload: Buffer,
  options: { salt?: Buffer; sender?: ECDH } = {}): Buffer {
  const receiver = unb64url(subscription.keys.p256dh);
  const secret = unb64url(subscription.keys.auth);
  if (receiver.length !== 65 || receiver[0] !== 4 || secret.length !== 16) throw new Error("Invalid push subscription keys");
  const salt = options.salt ?? randomBytes(16);
  // A fresh key pair per message, unless a test supplies one.
  const sender = options.sender ?? createECDH("prime256v1");
  const senderPublic = options.sender ? sender.getPublicKey() : sender.generateKeys();
  const shared = sender.computeSecret(receiver);
  const ikm = Buffer.from(hkdfSync("sha256", shared, secret,
    Buffer.concat([Buffer.from("WebPush: info\0"), receiver, senderPublic]), 32));
  const key = Buffer.from(hkdfSync("sha256", ikm, salt, Buffer.from("Content-Encoding: aes128gcm\0"), 16));
  const nonce = Buffer.from(hkdfSync("sha256", ikm, salt, Buffer.from("Content-Encoding: nonce\0"), 12));
  // One record: the payload, then the last-record delimiter.
  const cipher = createCipheriv("aes-128-gcm", key, nonce);
  const sealed = Buffer.concat([cipher.update(Buffer.concat([payload, Buffer.from([2])])), cipher.final(), cipher.getAuthTag()]);
  const header = Buffer.alloc(21);
  salt.copy(header, 0);
  header.writeUInt32BE(RECORD_SIZE, 16);
  header.writeUInt8(senderPublic.length, 20);
  return Buffer.concat([header, senderPublic, sealed]);
}

export type PushSender = (subscription: PushSubscription, payload: object, options: { urgent: boolean }) => Promise<number>;

/** Sends one encrypted push and answers the push service's status (201 sent; 404 or 410: the subscription is gone). */
export function webPushSender(vapid: () => VapidKey, subject: () => string, fetcher: typeof fetch = fetch): PushSender {
  return async (subscription, payload, { urgent }) => {
    const body = encryptPayload(subscription, Buffer.from(JSON.stringify(payload)));
    const response = await fetcher(subscription.endpoint, {
      method: "POST",
      headers: {
        Authorization: vapid().authorization(subscription.endpoint, subject()),
        "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
        TTL: urgent ? "600" : "3600", Urgency: urgent ? "high" : "normal",
      },
      body: new Uint8Array(body),
    });
    return response.status;
  };
}
