import { execFile, spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { Duplex } from "node:stream";
import { isAbsolute, join } from "node:path";
import { lstatSync, realpathSync } from "node:fs";
import type { AppshotInputOffer } from "./appshot-input.js";
import { appshotObject, appshotRule, decodeAppshotJSON } from "./appshot-protocol.js";
import {
  parseWindowsAppshotManifest, parseWindowsAppshotProcess, parseWindowsAppshotBrokerMessage,
  type WindowsAppshotBinding, type WindowsAppshotBrokerMessage, type WindowsAppshotProcess,
} from "./appshot-protocol-windows.js";

export type WindowsAppshotIdentity = WindowsAppshotProcess & {
  version: 2; platform: "windows"; monotonic_ns: string;
};
export type WindowsAppshotDescriptor = {
  version: 2; platform: "windows"; scope: string; instance_id: string; broker_nonce: string;
  process: WindowsAppshotProcess;
};
export type WindowsAppshotOffer = Extract<WindowsAppshotBrokerMessage, { type: "attach_offer" }>;
/** Native discovery/transport, not Node's unauthenticated named-pipe connection. */
export interface WindowsAppshotDependencies {
  /** False when the optional native helper cannot be resolved; absent means it can. */
  helperInstalled?(): boolean;
  identity(signal: AbortSignal): Promise<WindowsAppshotIdentity>;
  discover(signal: AbortSignal): Promise<WindowsAppshotDescriptor>;
  connect(descriptor: WindowsAppshotDescriptor, signal: AbortSignal): Promise<Duplex>;
  launch(signal: AbortSignal): Promise<void>;
  now(): bigint;
  verifyOffer(offer: WindowsAppshotOffer, recipient: WindowsAppshotBinding, signal: AbortSignal): Promise<AppshotInputOffer>;
}
const rejected = () => new Error("attachment_rejected");
export function validateWindowsAppshotIdentity(value: unknown, expectedPID: number): WindowsAppshotIdentity {
  const v = appshotObject(value, ["version", "platform", "pid", "process_start", "user_sid", "monotonic_ns"]);
  const p = parseWindowsAppshotProcess({ pid: v.pid, process_start: v.process_start, user_sid: v.user_sid });
  if (v.version !== 2 || v.platform !== "windows" || p.pid !== expectedPID) throw rejected();
  appshotRule(v.monotonic_ns, ["uint"]);
  return { version: 2, platform: "windows", ...p, monotonic_ns: v.monotonic_ns as string };
}
const sameProcess = (a: WindowsAppshotProcess, b: WindowsAppshotProcess) =>
  a.pid === b.pid && a.process_start === b.process_start && a.user_sid === b.user_sid;

/** Native child stdout is bounded and trusted code output, never a path read in JS.
 * The native reader pins directories/files, checks ACLs/IDs and proves its parent.
 * Revalidate its content and binding before retaining only draft metadata here.
 */
export function validateWindowsArtifactRead(value: unknown, offer: WindowsAppshotOffer,
  recipient: WindowsAppshotBinding): AppshotInputOffer {
  const parsed = parseWindowsAppshotBrokerMessage(JSON.stringify(offer));
  if (parsed.type !== "attach_offer" || offer.broker_id !== recipient.instance_id
    || offer.session_id !== recipient.session_id) throw rejected();
  parseWindowsAppshotProcess(recipient.recipient);
  const v = appshotObject(value, ["version", "platform", "manifest", "png_base64", "uia_json"]);
  if (v.version !== 2 || v.platform !== "windows" || typeof v.png_base64 !== "string"
    || typeof v.uia_json !== "string" || v.png_base64.length > 13981016) throw rejected();
  const m = parseWindowsAppshotManifest(JSON.stringify(v.manifest));
  if (!offer.manifest_path.endsWith(`/appshot-${m.token}.manifest.json`)
    || m.broker.instance_id !== recipient.instance_id || m.broker.session_id !== recipient.session_id
    || !sameProcess(m.broker.recipient, recipient.recipient)) throw rejected();
  const png = Buffer.from(v.png_base64, "base64"), uia = Buffer.from(v.uia_json, "utf8");
  const hash = (bytes: Buffer) => createHash("sha256").update(bytes).digest("hex");
  if (png.toString("base64") !== v.png_base64 || png.length !== m.png.size || uia.length !== m.uia.size
    || hash(png) !== m.png.sha256 || hash(uia) !== m.uia.sha256 || png.length < 33
    || !png.subarray(0, 8).equals(Buffer.from("89504e470d0a1a0a", "hex"))
    || png.readUInt32BE(8) !== 13 || png.toString("ascii", 12, 16) !== "IHDR"
    || png.readUInt32BE(16) !== m.png.width || png.readUInt32BE(20) !== m.png.height) throw rejected();
  return {
    requestId: offer.request_id, manifestPath: offer.manifest_path,
    appLabel: m.source.app_label, windowTitle: m.source.window_title,
    screenshotOnly: m.uia.coverage === "unavailable", binding: m.broker,
  };
}

/** Explicit helper path supplied by discovery/test harness; never auto-launches a service. */
export function windowsArtifactReader(executable: string): WindowsAppshotDependencies["verifyOffer"] {
  return async (offer, recipient, signal) => {
    if (signal.aborted) throw rejected();
    const value = await new Promise<unknown>((resolve, reject) => {
      execFile(executable, ["--appshot-read-artifact", offer.manifest_path, recipient.instance_id, recipient.session_id],
        { encoding: "utf8", windowsHide: true, timeout: 1400, maxBuffer: 16 * 1024 * 1024, signal },
        (error, stdout) => {
          if (error || signal.aborted) return reject(rejected());
          try { resolve(JSON.parse(stdout)); } catch { reject(rejected()); }
        });
    });
    if (signal.aborted) throw rejected();
    return validateWindowsArtifactRead(value, offer, recipient);
  };
}

export function validateWindowsDescriptor(value: unknown, scope: string): WindowsAppshotDescriptor {
  const v = appshotObject(value, ["version", "platform", "scope", "instance_id", "broker_nonce", "process"]);
  if (v.version !== 2 || v.platform !== "windows" || v.scope !== scope || !/^[a-z0-9-]{1,64}$/.test(scope)) throw rejected();
  appshotRule(v.instance_id, ["id"]); appshotRule(v.broker_nonce, ["id"]);
  return { version: 2, platform: "windows", scope, instance_id: v.instance_id as string,
    broker_nonce: v.broker_nonce as string, process: parseWindowsAppshotProcess(v.process) };
}

/** Discovery and artifact I/O is delegated to the native verified reader. */
export function windowsAppshotDependencies(executable: string, runtime: string, scope: string): WindowsAppshotDependencies {
  const query = (args: string[], signal: AbortSignal) => new Promise<unknown>((resolve, reject) => {
    if (signal.aborted || process.platform !== "win32") return reject(rejected());
    execFile(executable, args, { encoding: "buffer", windowsHide: true, timeout: 1400, maxBuffer: 65536, signal }, (error, stdout) => {
      if (error || signal.aborted) return reject(rejected());
      try { resolve(decodeAppshotJSON(stdout)); } catch { reject(rejected()); }
    });
  });
  return {
    identity: async signal => validateWindowsAppshotIdentity(await query(["--appshot-client-identity"], signal), process.pid),
    discover: async signal => validateWindowsDescriptor(await query(["--appshot-broker-identity", runtime, scope], signal), scope),
    connect: async (descriptor, signal) => {
      if (signal.aborted || process.platform !== "win32") throw rejected();
      validateWindowsDescriptor(descriptor, scope);
      const child = spawn(executable, ["--appshot-relay", runtime, scope, descriptor.instance_id, descriptor.broker_nonce,
        String(descriptor.process.pid), descriptor.process.process_start, descriptor.process.user_sid], {
        windowsHide: true, stdio: ["overlapped", "overlapped", "ignore"], signal,
      });
      const stream = new Duplex({
        read() { child.stdout!.resume(); },
        write(chunk, encoding, done) { child.stdin!.write(chunk, encoding, done); },
        final(done) { child.stdin!.end(done); },
        destroy(error, done) {
          child.stdin!.destroy(); child.stdout!.destroy();
          if (child.exitCode === null && child.signalCode === null) child.kill();
          done(error);
        },
      });
      child.stdout!.on("data", chunk => { if (!stream.push(chunk)) child.stdout!.pause(); });
      child.stdout!.on("end", () => stream.push(null));
      child.stdout!.on("error", () => stream.destroy(rejected()));
      child.stdin!.on("error", () => stream.destroy(rejected()));
      // Connect is considered ready only after the caller's nonce-bound hello.
      child.on("error", () => stream.destroy(rejected()));
      child.once("exit", () => stream.destroy());
      stream.on("error", () => {});
      await new Promise<void>((resolve, reject) => {
        child.once("spawn", resolve); child.once("error", () => reject(rejected()));
      });
      if (signal.aborted) { stream.destroy(); throw rejected(); }
      return stream;
    },
    launch: async signal => {
      if (signal.aborted || process.platform !== "win32") throw rejected();
      const child = spawn(executable, ["--appshot-daemon", runtime, scope], {
        windowsHide: true, detached: true, stdio: "ignore",
      });
      // Shared service outlives the launching TUI. The broker's authenticated
      // clients/idle grace own shutdown, not a single client's AbortSignal.
      await new Promise<void>((resolve, reject) => {
        child.once("spawn", () => { child.unref(); resolve(); });
        child.once("error", () => reject(rejected()));
      });
    },
    now: () => process.hrtime.bigint(), verifyOffer: windowsArtifactReader(executable),
  };
}

export function resolveWindowsAppshotHelper(environment: NodeJS.ProcessEnv = process.env): string {
  const override = environment.ASTRA_COMPUTER_HELPER_PATH?.trim();
  const root = environment.AGENT_PROJECT_ROOT;
  if (!override && !root) throw rejected();
  const path = override || join(root!, ".astra/bin/AstraWindowsComputerHelper/AstraWindowsComputerHelper.exe");
  if (!isAbsolute(path) || realpathSync(path).toLowerCase() !== path.toLowerCase()
    || !lstatSync(path).isFile() || lstatSync(path).isSymbolicLink()) throw rejected();
  const dll = join(path, "..", "AstraWindowsNative.dll");
  if (!lstatSync(dll).isFile() || lstatSync(dll).isSymbolicLink()) throw rejected();
  return path;
}

export function productionWindowsAppshotDependencies(environment: NodeJS.ProcessEnv = process.env): WindowsAppshotDependencies {
  // Resolve lazily inside start(): a missing optional helper must not crash Ink.
  let actual: WindowsAppshotDependencies | undefined;
  const get = () => {
    if (!actual) {
      if (!environment.LOCALAPPDATA || !isAbsolute(environment.LOCALAPPDATA)) throw rejected();
      actual = windowsAppshotDependencies(resolveWindowsAppshotHelper(environment),
        join(environment.LOCALAPPDATA, "AstraAppshot"), "production");
    }
    return actual;
  };
  return {
    helperInstalled: () => { try { get(); return true; } catch { return false; } },
    identity: signal => get().identity(signal), discover: signal => get().discover(signal),
    connect: (descriptor, signal) => get().connect(descriptor, signal), launch: signal => get().launch(signal),
    verifyOffer: (offer, recipient, signal) => get().verifyOffer(offer, recipient, signal),
    now: () => process.hrtime.bigint(),
  };
}
