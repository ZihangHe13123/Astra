import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { PhoneDevices } from "../src/main/phone-devices.js";
import { PhoneGateway } from "../src/main/phone-gateway.js";
import { PhoneClient, type DeviceStorage, type PhoneDeviceRecord } from "../src/phone/client.js";
import type { PhoneInvokeMethod } from "../src/phone-protocol.js";

/** A real gateway over temporary files, with the desktop's answers supplied by the test. */
export function memory(): DeviceStorage & { record?: PhoneDeviceRecord } {
  const storage: DeviceStorage & { record?: PhoneDeviceRecord } = {
    load: async () => storage.record, save: async record => { storage.record = record; }, clear: async () => { storage.record = undefined; },
  };
  return storage;
}

export async function setup(invoke: (device: string, method: PhoneInvokeMethod, args: unknown[]) => unknown = () => null) {
  const folder = mkdtempSync(join(tmpdir(), "astra-phone-gateway-"));
  const renderer = join(folder, "renderer"); mkdirSync(join(renderer, "assets"), { recursive: true });
  writeFileSync(join(renderer, "index.html"), '<!doctype html><html><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width"/><title>Astra</title></head><body><div id="root"></div><script type="module" src="./assets/index-1.js"></script></body></html>');
  writeFileSync(join(renderer, "assets", "index-1.js"), "console.log(1)");
  const site = join(folder, "phone"); mkdirSync(site);
  for (const name of ["bridge.js", "sw.js", "manifest.webmanifest", "icon-180.png"]) writeFileSync(join(site, name), name);
  writeFileSync(join(folder, "secret.txt"), "not served");
  const audio = join(folder, "phone-audio"); mkdirSync(audio);
  const devices = new PhoneDevices(join(folder, "gui", "phone-devices.json"));
  devices.setEnabled(true);
  const calls: { device: string; method: string; args: unknown[] }[] = [];
  const options = { staticRoot: site, rendererRoot: renderer, audioRoot: audio, port: 0,
    invoke: (device: string, method: PhoneInvokeMethod, args: unknown[]) => { calls.push({ device, method, args }); return invoke(device, method, args); } };
  const gateway = new PhoneGateway(devices, options);
  const port = await gateway.start();
  // What the desktop starts after a restart: a new gateway over the same paired phones and files.
  const restarted = () => new PhoneGateway(devices, options);
  return { devices, gateway, port, base: `http://127.0.0.1:${port}`, calls, audio, restarted };
}

export async function pairedClient(devices: PhoneDevices, base: string) {
  const client = new PhoneClient(memory(), base);
  const record = await client.pair(devices.startPairing().code, "iPhone");
  return { client, record, token: await client.signIn(record) };
}

export async function until(check: () => boolean, label: string) {
  const deadline = Date.now() + 3000;
  while (!check()) { if (Date.now() > deadline) throw new Error(`timed out: ${label}`); await new Promise(r => setTimeout(r, 10)); }
}

