import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PhoneAuthError, PhoneDevices } from "../src/main/phone-devices.js";
import { phoneAuthMessage } from "../src/phone-protocol.js";

async function phoneKey() {
  const keys = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, false, ["sign", "verify"]);
  return { keys, publicKey: await crypto.subtle.exportKey("jwk", keys.publicKey) };
}
async function sign(key: CryptoKey, nonce: string, deviceId: string) {
  const signature = await crypto.subtle.sign({ name: "ECDSA", hash: "SHA-256" }, key, new TextEncoder().encode(phoneAuthMessage(nonce, deviceId)));
  return Buffer.from(signature).toString("base64url");
}
function store(now = { value: 1_000_000 }) {
  const path = join(mkdtempSync(join(tmpdir(), "astra-phone-")), "gui", "phone-devices.json");
  return { path, now, devices: new PhoneDevices(path, () => now.value) };
}

test("a phone pairs once with the code shown in the window, and signs in with its own key", async () => {
  const { path, devices } = store();
  assert.throws(() => devices.pair("ABCD2345", "iPhone", {}), PhoneAuthError);
  const { code } = devices.startPairing();
  assert.match(code, /^[A-HJ-NP-Z2-9]{8}$/);
  const phone = await phoneKey();
  // Typed with a space or in lower case on the phone keyboard.
  const device = devices.pair(`${code.slice(0, 4).toLowerCase()} ${code.slice(4)}`, "  测试 iPhone  ", phone.publicKey);
  assert.equal(device.name, "测试 iPhone");
  assert.equal(devices.currentPairing(), undefined);
  assert.throws(() => devices.pair(code, "again", phone.publicKey), PhoneAuthError, "a code works once");

  const nonce = devices.challenge(device.id);
  const { token } = devices.signIn(device.id, nonce, await sign(phone.keys.privateKey, nonce, device.id));
  assert.equal(devices.deviceForToken(token), device.id);
  assert.ok(devices.list()[0].lastSeen);
  // The file holds public keys only, readable by the user alone.
  const saved = readFileSync(path, "utf8");
  assert.doesNotMatch(saved, /"d"/);
  assert.equal(statSync(path).mode & 0o777, 0o600);
});

test("a challenge works once, only for its device, and a wrong key or text is refused", async () => {
  const { devices } = store();
  const first = await phoneKey(), second = await phoneKey();
  let { code } = devices.startPairing();
  const a = devices.pair(code, "A", first.publicKey);
  ({ code } = devices.startPairing());
  const b = devices.pair(code, "B", second.publicKey);

  let nonce = devices.challenge(a.id);
  assert.throws(() => devices.signIn(a.id, nonce, "AAAA"), PhoneAuthError);
  assert.throws(() => devices.signIn(a.id, nonce, ""), PhoneAuthError, "the failed try used the challenge up");
  nonce = devices.challenge(a.id);
  const asB = await sign(second.keys.privateKey, nonce, b.id);
  assert.throws(() => devices.signIn(b.id, nonce, asB), PhoneAuthError, "A's challenge is not B's");
  nonce = devices.challenge(a.id);
  const signed = await sign(second.keys.privateKey, nonce, a.id);
  assert.throws(() => devices.signIn(a.id, nonce, signed), PhoneAuthError, "B's key cannot sign in as A");
  assert.throws(() => devices.challenge("not-a-device"), PhoneAuthError);
});

test("codes and challenges expire, wrong codes withdraw the code, and only P-256 public keys are taken", async () => {
  const { devices, now } = store();
  const phone = await phoneKey();
  let { code } = devices.startPairing();
  now.value += 301_000;
  assert.throws(() => devices.pair(code, "late", phone.publicKey), PhoneAuthError);

  ({ code } = devices.startPairing());
  for (let i = 0; i < 5; i++) assert.throws(() => devices.pair("WRONGXYZ", "guess", phone.publicKey), PhoneAuthError);
  assert.equal(devices.currentPairing(), undefined);
  assert.throws(() => devices.pair(code, "after five wrong", phone.publicKey), PhoneAuthError);

  ({ code } = devices.startPairing());
  const exportable = await crypto.subtle.generateKey({ name: "ECDSA", namedCurve: "P-256" }, true, ["sign", "verify"]);
  assert.throws(() => devices.pair(code, "private", { ...phone.publicKey, d: (exportable.privateKey as any).d ?? "x" }), PhoneAuthError);
  assert.throws(() => devices.pair(code, "rsa", { kty: "RSA", n: "x", e: "AQAB" }), PhoneAuthError);
  const device = devices.pair(code, "ok", phone.publicKey);

  const nonce = devices.challenge(device.id);
  const signature = await sign(phone.keys.privateKey, nonce, device.id);
  now.value += 61_000;
  assert.throws(() => devices.signIn(device.id, nonce, signature), PhoneAuthError, "a challenge lasts a minute");
});

test("removing a phone ends its tokens at once and survives a restart; paired phones survive too", async () => {
  const { path, devices, now } = store();
  const removed: string[] = [];
  devices.onRevoked(id => removed.push(id));
  devices.setEnabled(true);
  const phone = await phoneKey(), other = await phoneKey();
  let { code } = devices.startPairing();
  const kept = devices.pair(code, "kept", other.publicKey);
  ({ code } = devices.startPairing());
  const device = devices.pair(code, "lost", phone.publicKey);
  const nonce = devices.challenge(device.id);
  const { token } = devices.signIn(device.id, nonce, await sign(phone.keys.privateKey, nonce, device.id));

  assert.equal(devices.revoke(device.id), true);
  assert.deepEqual(removed, [device.id]);
  assert.equal(devices.deviceForToken(token), undefined);
  assert.throws(() => devices.challenge(device.id), PhoneAuthError);

  const reopened = new PhoneDevices(path, () => now.value);
  assert.equal(reopened.enabled, true);
  assert.deepEqual(reopened.list().map(item => item.id), [kept.id]);
  const again = reopened.challenge(kept.id);
  assert.ok(reopened.signIn(kept.id, again, await sign(other.keys.privateKey, again, kept.id)).token);
  // A token lasts twelve hours.
  const fresh = reopened.challenge(kept.id);
  const late = reopened.signIn(kept.id, fresh, await sign(other.keys.privateKey, fresh, kept.id));
  now.value += 12 * 3600 * 1000 + 1;
  assert.equal(reopened.deviceForToken(late.token), undefined);
});
