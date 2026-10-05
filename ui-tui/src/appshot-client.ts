import { EventEmitter } from "node:events";
import { execFile, spawn } from "node:child_process";
import {
  constants,
  lstatSync,
  openSync,
  fstatSync,
  readSync,
  closeSync,
  realpathSync,
} from "node:fs";
import { isAbsolute, join, dirname, parse } from "node:path";
import { createConnection, type Socket } from "node:net";
import { randomUUID } from "node:crypto";
import type { Duplex } from "node:stream";
import type { AppshotInputOffer } from "./appshot-input.js";
import {
  WindowsAppshotFrameDecoder, parseWindowsAppshotBrokerMessage, encodeWindowsAppshotMessage,
  type WindowsAppshotBinding, type WindowsAppshotBrokerMessage, type WindowsAppshotClientMessage,
} from "./appshot-protocol-windows.js";
import {
  type WindowsAppshotDependencies, type WindowsAppshotIdentity, type WindowsAppshotDescriptor,
  validateWindowsAppshotIdentity,
} from "./appshot-windows.js";
import {
  AppshotFrameDecoder,
  encodeAppshotMessage,
  parseAppshotBrokerMessage,
  type AppshotAttachOffer,
  type AppshotAttachCommit,
  type AppshotClientMessage,
  type AppshotCommandResult,
  type AppshotBrokerBinding,
} from "./appshot-protocol.js";
export const APPSHOT_ERROR_CODES = [
  "shortcut_conflict",
  "no_receiving_session",
  "receiving_session_ambiguous",
  "attachment_limit_reached",
  "capture_in_progress",
  "permission_unavailable",
  "protected_ui",
  "source_window_unavailable",
  "source_window_changed",
  "capture_failed",
  "capture_timeout",
  "capture_cancelled",
  "ax_observation_failed",
  "artifact_unsafe",
  "recipient_disconnected",
  "attachment_rejected",
  "backend_busy",
  "context_budget_exceeded",
  "submission_unknown",
  "submission_conflict",
  "media_unavailable",
  "resource_limit_reached",
  "settings_invalid",
  "settings_write_failed",
  "protocol_invalid",
  "broker_unavailable",
] as const;
/** `notice` event codes. `helper_unavailable` is client-only: a helper that is not installed cannot report it. */
export type AppshotNotice =
  | "recipient_disconnected"
  | "broker_unavailable"
  | "helper_unavailable";
export type AppshotClientSnapshot = {
  connection: "disconnected" | "connecting" | "connected";
  enabled: boolean;
  chord: string;
  registration: "registered" | "conflict" | "unavailable";
  connectedTuis: number;
  permission: "ready" | "unavailable" | "unknown";
};
export type AppshotDraftState = {
  appshotCount: number;
  canAccept: boolean;
};
/** Synchronous transactional consumer: stage reserves locally, commit installs before returning current totals. */
export interface AppshotConsumer {
  stage(offer: AppshotAttachOffer, recipient: AppshotBrokerBinding): boolean;
  /** Only called after the native Windows read bridge verifies files and recipient. */
  stageWindows?(offer: AppshotInputOffer): boolean;
  commit(commit: AppshotAttachCommit | Extract<WindowsAppshotBrokerMessage, { type: "attach_commit" }>): AppshotDraftState;
  revoke(event: { requestID: string; reason: string }): void;
  /** Invalidate staged/editable sendability, retain frozen backend submissions for explicit reconciliation. */
  disconnect(event: {
    reason: "recipient_disconnected";
    unsent: "revoked";
    pending: "unknown";
  }): AppshotDraftState | void;
}
type Identity = {
  pid: number;
  uid: number;
  process_start: string;
  monotonic_ns: string;
};
type Descriptor = {
  instance_id: string;
  broker_nonce: string;
  socketPath: string;
};
export interface AppshotDependencies {
  /** False when the optional native helper cannot be resolved; absent means it can. */
  helperInstalled?(): boolean;
  currentUID(): number;
  identity(signal: AbortSignal): Promise<Identity>;
  discover(signal: AbortSignal): Promise<Descriptor>;
  connect(path: string, signal: AbortSignal): Promise<Socket>;
  launch(signal: AbortSignal): Promise<void>;
  now(): bigint;
}
const unavailable = () => new Error("broker_unavailable");
const uint = (v: unknown): v is string =>
  typeof v === "string" &&
  /^(0|[1-9][0-9]*)$/.test(v) &&
  BigInt(v) <= 18446744073709551615n;
const exact = (value: any, keys: string[]) =>
  value &&
  typeof value === "object" &&
  !Array.isArray(value) &&
  Object.keys(value).sort().join() === keys.sort().join();
export function resolveAppshotHelper(
  environment: NodeJS.ProcessEnv = process.env,
): string {
  const override = environment.ASTRA_COMPUTER_HELPER_PATH?.trim();
  const root = environment.AGENT_PROJECT_ROOT;
  if (!override && !root) throw unavailable();
  const candidate =
    override ||
    join(
      realpathSync(root!),
      ".astra/bin/AstraMacComputerHelper.app/Contents/MacOS/AstraMacComputerHelper",
    );
  if (!isAbsolute(candidate) || realpathSync(candidate) !== candidate)
    throw unavailable();
  const s = lstatSync(candidate);
  if (!s.isFile() || s.uid !== process.getuid?.() || !(s.mode & 0o100))
    throw unavailable();
  return candidate;
}
function helperJSON(
  path: string,
  mode: string,
  signal: AbortSignal,
): Promise<any> {
  return new Promise((resolve, reject) =>
    execFile(
      path,
      [mode],
      { encoding: "utf8", timeout: 1500, maxBuffer: 65536, signal },
      (error, stdout) => {
        if (error) return reject(unavailable());
        try {
          resolve(JSON.parse(stdout));
        } catch {
          reject(unavailable());
        }
      },
    ),
  );
}
/** Validate fixed runtime hierarchy, held descriptor inode and journaled socket before native live identity proof. */
export function readAppshotDescriptor(runtime: string, uid: number): any {
  if (!isAbsolute(runtime) || realpathSync(runtime) !== runtime)
    throw unavailable();
  let part = runtime;
  while (part !== parse(part).root) {
    const s = lstatSync(part);
    if (!s.isDirectory() || s.isSymbolicLink()) throw unavailable();
    part = dirname(part);
  }
  const dir = lstatSync(runtime, { bigint: true });
  if (dir.uid !== BigInt(uid) || (dir.mode & 4095n) !== 448n)
    throw unavailable();
  const path = join(runtime, "broker.json");
  const fd = openSync(
    path,
    constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK,
  );
  try {
    const stat = fstatSync(fd, { bigint: true });
    if (
      !stat.isFile() ||
      stat.uid !== BigInt(uid) ||
      (stat.mode & 4095n) !== 384n ||
      stat.nlink !== 1n ||
      stat.size > 65536n
    )
      throw unavailable();
    const bytes = Buffer.alloc(65537);
    let length = 0,
      read = 0;
    while (
      length < bytes.length &&
      (read = readSync(fd, bytes, length, bytes.length - length, null)) > 0
    )
      length += read;
    if (length > 65536) throw unavailable();
    const raw = new TextDecoder("utf-8", { fatal: true }).decode(
      bytes.subarray(0, length),
    );
    const d = JSON.parse(raw);
    if (
      !exact(d, [
        "schema_version",
        "instance_id",
        "broker_nonce",
        "pid",
        "uid",
        "process_start",
        "socket_name",
        "entries",
      ]) ||
      d.schema_version !== 1 ||
      d.uid !== uid ||
      !Number.isInteger(d.pid) ||
      d.pid < 1 ||
      d.pid > 2147483647 ||
      !uint(d.process_start) ||
      d.socket_name !== "broker.sock" ||
      !Array.isArray(d.entries) ||
      d.entries.length > 1024
    )
      throw unavailable();
    const uuid =
      /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;
    if (!uuid.test(d.instance_id) || !uuid.test(d.broker_nonce))
      throw unavailable();
    // Native publishes exact sorted canonical JSON; comparison rejects duplicate keys and alternate encodings.
    const canonical = (v: any): any =>
      Array.isArray(v)
        ? v.map(canonical)
        : v && typeof v === "object"
          ? Object.fromEntries(
              Object.keys(v)
                .sort()
                .map((k) => [k, canonical(v[k])]),
            )
          : v;
    if (raw !== JSON.stringify(canonical(d))) throw unavailable();
    const names = new Set();
    for (const e of d.entries) {
      if (
        !exact(e, ["name", "device", "inode", "kind"]) ||
        !uint(e.device) ||
        !uint(e.inode) ||
        names.has(e.name) ||
        !(
          (e.name === "broker.sock" && e.kind === "socket") ||
          (/^appshot-[a-f0-9]{32}\.(png|ax\.json|manifest\.json)$/.test(
            e.name,
          ) &&
            e.kind === "file")
        )
      )
        throw unavailable();
      names.add(e.name);
    }
    const entry = d.entries.find((e: any) => e.name === "broker.sock");
    const socket = lstatSync(join(runtime, "broker.sock"), { bigint: true });
    if (
      !entry ||
      !socket.isSocket() ||
      socket.uid !== BigInt(uid) ||
      socket.nlink !== 1n ||
      (socket.mode & 4095n) !== 384n ||
      socket.dev.toString() !== entry.device ||
      socket.ino.toString() !== entry.inode
    )
      throw unavailable();
    const after = lstatSync(path, { bigint: true }),
      afterDir = lstatSync(runtime, { bigint: true });
    if (
      after.dev !== stat.dev ||
      after.ino !== stat.ino ||
      afterDir.dev !== dir.dev ||
      afterDir.ino !== dir.ino
    )
      throw unavailable();
    return d;
  } finally {
    closeSync(fd);
  }
}
export function productionAppshotDependencies(
  environment: NodeJS.ProcessEnv = process.env,
): AppshotDependencies {
  let helper: string;
  const executable = () => (helper = resolveAppshotHelper(environment));
  return {
    helperInstalled: () => {
      try {
        executable();
        return true;
      } catch {
        return false;
      }
    },
    currentUID: () => process.getuid!(),
    now: () => process.hrtime.bigint(),
    identity: async (signal) =>
      helperJSON(executable(), "--appshot-client-identity", signal),
    discover: async (signal) => {
      const uid = process.getuid!(),
        runtime = `/private/tmp/astra-appshot-${uid}`;
      const d = readAppshotDescriptor(runtime, uid);
      const live = await helperJSON(
        executable(),
        "--appshot-broker-identity",
        signal,
      );
      if (
        !exact(live, [
          "instance_id",
          "broker_nonce",
          "pid",
          "uid",
          "process_start",
        ]) ||
        ["instance_id", "broker_nonce", "pid", "uid", "process_start"].some(
          (k) => live[k] !== d[k],
        )
      )
        throw unavailable();
      const after = readAppshotDescriptor(runtime, uid);
      if (
        ["instance_id", "broker_nonce", "pid", "uid", "process_start"].some(
          (k) => after[k] !== d[k],
        )
      )
        throw unavailable();
      const socketEntry = (value: any) =>
        value.entries.find((entry: any) => entry.kind === "socket");
      if (JSON.stringify(socketEntry(d)) !== JSON.stringify(socketEntry(after)))
        throw unavailable();
      return { ...d, socketPath: join(runtime, "broker.sock") };
    },
    connect: (path, signal) =>
      new Promise((resolve, reject) => {
        const socket = createConnection({ path });
        const abort = () => socket.destroy(unavailable());
        signal.addEventListener("abort", abort, { once: true });
        socket.setTimeout(1500, abort);
        socket.once("error", reject);
        socket.once("connect", () => {
          socket.setTimeout(0);
          resolve(socket);
        });
        socket.once("close", () => signal.removeEventListener("abort", abort));
      }),
    launch: async (signal) => {
      if (signal.aborted) throw unavailable();
      const child = spawn(executable(), ["--appshot-daemon"], {
        detached: true,
        stdio: "ignore",
      });
      await new Promise<void>((resolve, reject) => {
        child.once("error", () => reject(unavailable()));
        child.once("spawn", resolve);
      });
      child.unref();
    },
  };
}
export class AppshotClient extends EventEmitter {
  state: AppshotClientSnapshot = {
    connection: "disconnected",
    enabled: false,
    chord: "",
    registration: "unavailable",
    connectedTuis: 0,
    permission: "unknown",
  };
  private deps: AppshotDependencies;
  private abort = new AbortController();
  private socket?: Duplex;
  private descriptor?: Descriptor | WindowsAppshotDescriptor;
  private windowsIdentity?: WindowsAppshotIdentity;
  private session = randomUUID();
  private processStart?: string;
  private activity = 0n;
  private offset = 0n;
  private calibrated = false;
  private lastInput?: bigint;
  private draft: AppshotDraftState = { appshotCount: 0, canAccept: false };
  private staged = new Map<string, AppshotAttachOffer | Extract<WindowsAppshotBrokerMessage, { type: "attach_offer" }>>();
  private verifying = new Map<string, AbortController>();
  private committed = new Set<string>();
  private heartbeat?: ReturnType<typeof setInterval>;
  private stableTimer?: ReturnType<typeof setTimeout>;
  private reconnectAttempts = 0;
  private reconnectEpoch = 0;
  private disconnectNotified = false;
  private gaveUp = new Set<AppshotNotice>();
  // "missing" only while this client has never resolved the optional helper.
  private helper: "unknown" | "missing" | "present" = "unknown";
  private starting?: Promise<void>;
  private pending = new Map<
    string,
    {
      resolve: (r: AppshotCommandResult | Extract<WindowsAppshotBrokerMessage, { type: "command_result" }>) => void;
      timer: ReturnType<typeof setTimeout>;
    }
  >();
  constructor(
    private options: {
      platform?: string;
      deps?: AppshotDependencies;
      windowsDeps?: WindowsAppshotDependencies;
      consumer?: AppshotConsumer;
      heartbeatMS?: number;
      retryMS?: number;
    } = {},
  ) {
    super();
    this.deps = options.deps ?? productionAppshotDependencies();
  }
  private changed() {
    this.emit("change", this.state);
  }
  start(): Promise<void> {
    if (
      this.state.connection === "connected" ||
      this.abort.signal.aborted ||
      !((this.options.platform ?? process.platform) === "darwin" && !this.options.windowsDeps
        || (this.options.platform ?? process.platform) === "win32" && !!this.options.windowsDeps)
    )
      return Promise.resolve();
    return (this.starting ??= this.run().finally(() => {
      this.starting = undefined;
    }));
  }
  private async pause(ms: number) {
    if (this.abort.signal.aborted) return;
    await new Promise<void>((resolve) => {
      const done = () => {
        clearTimeout(timer);
        this.abort.signal.removeEventListener("abort", done);
        resolve();
      };
      const timer = setTimeout(done, ms);
      this.abort.signal.addEventListener("abort", done, { once: true });
    });
  }
  /** Automatic retrying has stopped: say so once, until a connection is established again. */
  private giveUp(code: Exclude<AppshotNotice, "recipient_disconnected">) {
    if (this.abort.signal.aborted || this.gaveUp.has(code)) return;
    this.gaveUp.add(code);
    this.emit("notice", code);
  }
  private helperAbsent() {
    try {
      return (this.options.windowsDeps ?? this.deps).helperInstalled?.() === false;
    } catch {
      return false; // An unusable probe decides nothing; the ordinary start reports the failure.
    }
  }
  private async run() {
    // An optional helper that was never there leaves nothing to launch, so input has nothing to
    // retry. One that vanishes later is an ordinary failure below, and input keeps retrying it.
    if (this.helper !== "present") {
      this.helper = this.helperAbsent() ? "missing" : "present";
      if (this.helper === "missing") return this.giveUp("helper_unavailable");
    }
    this.state = { ...this.state, connection: "connecting" };
    this.changed();
    let launched = false;
    try {
      const windows = this.options.windowsDeps;
      const id = windows ? await windows.identity(this.abort.signal) : await this.deps.identity(this.abort.signal);
      if (windows) {
        this.windowsIdentity = validateWindowsAppshotIdentity(id, process.pid);
      } else if (
        !exact(id, ["pid", "uid", "process_start", "monotonic_ns"]) ||
        id.pid !== process.pid ||
        !("uid" in id) || id.uid !== this.deps.currentUID() ||
        !uint(id.process_start) ||
        !uint(id.monotonic_ns)
      )
        throw unavailable();
      this.processStart = id.process_start;
      this.offset = BigInt(id.monotonic_ns) - (windows ?? this.deps).now();
      this.calibrated = true;
      // A new connection gets a new clock calibration. Never retain a future
      // value from the previous epoch (e.g. after sleep/clock-domain drift).
      const calibratedInput = this.lastInput === undefined ? 0n : this.lastInput + this.offset;
      this.activity = calibratedInput > 0n ? calibratedInput : 0n;
      for (
        let attempt = 0;
        attempt < 5 && !this.abort.signal.aborted;
        attempt++
      ) {
        try {
          const d = windows ? await windows.discover(this.abort.signal) : await this.deps.discover(this.abort.signal);
          const socket = windows ? await windows.connect(d as WindowsAppshotDescriptor, this.abort.signal)
            : await this.deps.connect((d as Descriptor).socketPath, this.abort.signal);
          if (this.abort.signal.aborted) {
            socket.destroy();
            return;
          }
          await this.handshake(socket, d, id);
          return;
        } catch {
          this.socket?.destroy();
          if (!launched && !this.abort.signal.aborted) {
            launched = true;
            try {
              await (windows ?? this.deps).launch(this.abort.signal);
            } catch {}
          }
          await this.pause((this.options.retryMS ?? 100) * (attempt + 1));
        }
      }
    } catch {}
    if (!this.abort.signal.aborted) {
      this.state = { ...this.state, connection: "disconnected" };
      this.changed();
      this.giveUp("broker_unavailable");
    }
  }
  private handshake(
    socket: Duplex,
    d: Descriptor | WindowsAppshotDescriptor,
    id: Identity | WindowsAppshotIdentity,
  ): Promise<void> {
    this.socket = socket;
    this.descriptor = d;
    this.session = randomUUID();
    const decoder = this.options.windowsDeps ? new WindowsAppshotFrameDecoder() : new AppshotFrameDecoder();
    return new Promise((resolve, reject) => {
      let ready = false;
      const timer = setTimeout(() => {
        socket.destroy();
        reject(unavailable());
      }, 1500);
      socket.on("data", (chunk: Buffer) => {
        try {
          for (const raw of decoder.feed(chunk)) {
            const m = this.options.windowsDeps ? parseWindowsAppshotBrokerMessage(JSON.stringify(raw))
              : parseAppshotBrokerMessage(JSON.stringify(raw));
            if (!ready) {
              if (
                m.type !== "hello_ack" ||
                m.instance_id !== d.instance_id ||
                m.broker_nonce !== d.broker_nonce ||
                m.session_id !== this.session
              )
                throw unavailable();
              ready = true;
              clearTimeout(timer);
              this.gaveUp.clear();
              this.state = { ...this.state, connection: "connected" };
              this.sendState();
              this.heartbeat = setInterval(
                () => this.sendState(),
                this.options.heartbeatMS ?? 1000,
              );
              this.heartbeat.unref();
              this.stableTimer = setTimeout(() => {
                if (this.socket !== socket) return;
                this.reconnectAttempts = 0;
                this.disconnectNotified = false;
              }, 10000);
              this.stableTimer.unref();
              this.changed();
              resolve();
            } else {
              if (
                m.type === "hello_ack" ||
                m.broker_id !== d.instance_id ||
                m.session_id !== this.session
              )
                throw unavailable();
              this.receive(m);
            }
          }
        } catch {
          socket.destroy();
        }
      });
      socket.on("error", () => {});
      socket.once("close", () => {
        clearTimeout(timer);
        if (this.socket !== socket) return;
        this.socket = undefined;
        clearInterval(this.heartbeat);
        clearTimeout(this.stableTimer);
        this.staged.clear();
        for (const controller of this.verifying.values()) controller.abort();
        this.verifying.clear();
        this.committed.clear();
        for (const p of this.pending.values()) {
          clearTimeout(p.timer);
          p.resolve(this.failure());
        }
        this.pending.clear();
        if (ready) {
          try {
            const retained = this.options.consumer?.disconnect({
              reason: "recipient_disconnected",
              unsent: "revoked",
              pending: "unknown",
            });
            this.draft = {
              appshotCount: retained?.appshotCount ?? 0,
              canAccept: false,
            };
          } catch {
            this.draft = { appshotCount: 4, canAccept: false };
          }
          this.state = { ...this.state, connection: "disconnected" };
          this.changed();
          if (!this.abort.signal.aborted && !this.disconnectNotified) {
            this.disconnectNotified = true;
            this.emit("notice", "recipient_disconnected");
          }
          // Input can reconnect and drop again while an earlier reconnect still waits:
          // only the newest disconnect decides what happens next.
          const epoch = ++this.reconnectEpoch;
          if (!this.abort.signal.aborted && this.reconnectAttempts < 5) {
            const delay = Math.min(5000, (this.options.retryMS ?? 500) * 2 ** this.reconnectAttempts++);
            queueMicrotask(async () => {
              const previous = this.starting;
              if (previous) await previous;
              await this.pause(delay);
              if (!this.abort.signal.aborted && epoch === this.reconnectEpoch) void this.start();
            });
          } else this.giveUp("broker_unavailable");
        } else reject(unavailable());
      });
      this.write({
        type: "hello",
        ...(this.options.windowsDeps ? { version: 2, platform: "windows", user_sid: this.windowsIdentity!.user_sid }
          : { version: 1 }),
        session_id: this.session,
        pid: id.pid,
        process_start: id.process_start,
        client_nonce: d.broker_nonce,
      });
    });
  }
  private binding(request_id: string) {
    return {
      ...(this.options.windowsDeps ? { version: 2 as const, platform: "windows" as const } : { version: 1 as const }),
      request_id,
      broker_id: this.descriptor!.instance_id,
      session_id: this.session,
    };
  }
  private write(message: Record<string, unknown>) {
    if (this.socket) {
      if (this.socket.writableLength > 262144) throw unavailable();
      this.socket.write(this.options.windowsDeps
        ? encodeWindowsAppshotMessage(message as WindowsAppshotClientMessage)
        : encodeAppshotMessage(message as unknown as AppshotClientMessage));
    }
  }
  private sendState(requestID: string = randomUUID()) {
    if (this.state.connection !== "connected") return;
    try {
      this.write({
        type: "client_state",
        ...this.binding(requestID),
        activity_ns: this.activity.toString(),
        appshot_count: this.draft.appshotCount,
        can_accept:
          !!this.options.consumer &&
          this.draft.canAccept &&
          this.draft.appshotCount < 4,
      });
    } catch {
      this.socket?.destroy();
    }
  }
  get recipientBinding(): AppshotBrokerBinding | WindowsAppshotBinding | undefined {
    if (
      this.state.connection !== "connected" ||
      !this.processStart ||
      !this.descriptor
    )
      return undefined;
    if (this.options.windowsDeps && this.windowsIdentity) return {
      instance_id: this.descriptor.instance_id, session_id: this.session,
      recipient: { pid: this.windowsIdentity.pid, process_start: this.windowsIdentity.process_start,
        user_sid: this.windowsIdentity.user_sid },
    };
    return {
      instance_id: this.descriptor.instance_id,
      session_id: this.session,
      process_start: this.processStart,
    };
  }
  get activityNS() {
    return this.activity;
  }
  recordInput() {
    this.lastInput = (this.options.windowsDeps ?? this.deps).now();
    // Input retries a broker that went away, never a helper that was not installed to begin with.
    if (this.state.connection === "disconnected" && !this.starting && this.helper !== "missing" && !this.abort.signal.aborted) {
      this.reconnectAttempts = 0;
      this.disconnectNotified = false;
      void this.start();
    }
    if (!this.calibrated) return;
    const time = this.lastInput + this.offset;
    if (time > this.activity && time >= 0n && time <= 18446744073709551615n) {
      this.activity = time;
      this.sendState();
    }
  }
  updateDraft(activityNS: bigint, appshotCount: number, canAccept: boolean) {
    if (
      activityNS < this.activity ||
      activityNS < 0n ||
      activityNS > 18446744073709551615n ||
      !Number.isInteger(appshotCount) ||
      appshotCount < 0 ||
      appshotCount > 4
    )
      throw unavailable();
    this.activity = activityNS;
    this.draft = { appshotCount, canAccept };
    this.sendState();
  }
  private receive(m: ReturnType<typeof parseAppshotBrokerMessage> | WindowsAppshotBrokerMessage) {
    switch (m.type) {
      case "attach_offer": {
        if (m.version === 2) {
          this.verifyWindowsOffer(m as Extract<WindowsAppshotBrokerMessage, { type: "attach_offer" }>);
          break;
        }
        if (this.staged.has(m.request_id) || this.committed.has(m.request_id) || this.verifying.has(m.request_id))
          throw unavailable();
        let accepted = false;
        if (
          this.options.consumer &&
          this.draft.canAccept &&
          this.draft.appshotCount + this.staged.size < 4
        ) {
          accepted = this.options.consumer.stage(m, this.recipientBinding! as AppshotBrokerBinding);
          if (accepted) this.staged.set(m.request_id, m);
        }
        this.write({
          type: "attach_ack",
          ...this.binding(m.request_id),
          accepted,
          reason: accepted ? "" : "attachment_rejected",
        });
        break;
      }
      case "attach_commit": {
        const offer = this.staged.get(m.request_id);
        if (
          !offer ||
          offer.manifest_path !== m.manifest_path ||
          !this.options.consumer
        )
          throw unavailable();
        const next = this.options.consumer.commit(m);
        this.staged.delete(m.request_id);
        this.committed.add(m.request_id);
        if (
          !Number.isInteger(next.appshotCount) ||
          next.appshotCount < 0 ||
          next.appshotCount > 4
        )
          throw unavailable();
        this.draft = next;
        this.sendState(m.request_id);
        break;
      }
      case "attach_revoke":
        this.verifying.get(m.request_id)?.abort();
        this.verifying.delete(m.request_id);
        this.staged.delete(m.request_id);
        this.committed.delete(m.request_id);
        this.options.consumer?.revoke({
          requestID: m.request_id,
          reason: APPSHOT_ERROR_CODES.includes(m.reason as any)
            ? m.reason
            : "attachment_rejected",
        });
        break;
      case "status":
        this.state = {
          connection: "connected",
          enabled: m.enabled,
          chord: m.chord,
          registration: m.registration,
          connectedTuis: m.connected_tuis,
          permission: m.permission,
        };
        this.changed();
        {
          const pending = this.pending.get(m.request_id);
          if (pending) {
            clearTimeout(pending.timer);
            this.pending.delete(m.request_id);
            pending.resolve({
              type: "command_result",
              ...this.binding(m.request_id),
              ok: true,
              code: "ok",
              message: "",
            });
          }
        }
        break;
      case "command_result": {
        const p = this.pending.get(m.request_id);
        if (p) {
          clearTimeout(p.timer);
          this.pending.delete(m.request_id);
          p.resolve({
            ...m,
            message: "",
            code: m.ok
              ? "ok"
              : APPSHOT_ERROR_CODES.includes(m.code as any)
                ? m.code
                : "broker_unavailable",
          });
        }
        break;
      }
      case "release_ack":
        this.committed.delete(m.request_id);
        break;
      case "client_state_ack":
        break;
      default:
        throw unavailable();
    }
  }
  release(requestID: string) {
    if (!this.committed.has(requestID)) return;
    try {
      this.write({ type: "release", ...this.binding(requestID) });
    } catch {
      this.socket?.destroy();
    }
  }
  private failure(): AppshotCommandResult | Extract<WindowsAppshotBrokerMessage, { type: "command_result" }> {
    return {
      type: "command_result",
      ...(this.options.windowsDeps ? { version: 2 as const, platform: "windows" as const } : { version: 1 as const }),
      request_id: "unavailable",
      broker_id: "unavailable",
      session_id: this.session,
      ok: false,
      code: "broker_unavailable",
      message: "",
    };
  }
  command(
    name: "status" | "shortcut" | "enable" | "disable",
    argument: string,
  ): Promise<AppshotCommandResult | Extract<WindowsAppshotBrokerMessage, { type: "command_result" }>> {
    if (this.state.connection !== "connected" || this.pending.size >= 16)
      return Promise.resolve(this.failure());
    const request = randomUUID();
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.pending.delete(request);
        resolve(this.failure());
      }, 2000);
      this.pending.set(request, { resolve, timer });
      try {
        this.write({
          type: "command",
          ...this.binding(request),
          name,
          argument,
        });
      } catch {
        clearTimeout(timer);
        this.pending.delete(request);
        resolve(this.failure());
      }
    });
  }
  async close() {
    this.abort.abort();
    for (const controller of this.verifying.values()) controller.abort();
    this.verifying.clear();
    clearInterval(this.heartbeat);
    clearTimeout(this.stableTimer);
    this.socket?.destroy();
    await this.starting;
    this.state = { ...this.state, connection: "disconnected" };
  }

  private verifyWindowsOffer(offer: Extract<WindowsAppshotBrokerMessage, { type: "attach_offer" }>) {
    const dependencies = this.options.windowsDeps, socket = this.socket, recipient = this.recipientBinding;
    if (!dependencies || !socket || !recipient || !("recipient" in recipient)
      || this.staged.has(offer.request_id) || this.committed.has(offer.request_id) || this.verifying.has(offer.request_id))
      throw unavailable();
    const acknowledge = (accepted: boolean) => this.write({ type: "attach_ack", ...this.binding(offer.request_id),
      accepted, reason: accepted ? "" : "attachment_rejected" });
    if (!this.options.consumer?.stageWindows || !this.draft.canAccept
      || this.draft.appshotCount + this.staged.size + this.verifying.size >= 4) { acknowledge(false); return; }
    const controller = new AbortController();
    this.verifying.set(offer.request_id, controller);
    const timer = setTimeout(() => controller.abort(), 1500);
    void (async () => {
      let accepted = false;
      let owned = false;
      let abortRead: (() => void) | undefined;
      try {
        // A dependency must kill its child on abort; the race also bounds our
        // reservation even if a faulty dependency ignores the signal.
        const aborted = new Promise<never>((_, reject) => {
          abortRead = () => reject(unavailable());
          controller.signal.addEventListener("abort", abortRead, { once: true });
        });
        const verified = await Promise.race([
          dependencies.verifyOffer(offer, recipient, controller.signal), aborted,
        ]);
        if (controller.signal.aborted || this.abort.signal.aborted || this.socket !== socket
          || this.session !== recipient.session_id || this.state.connection !== "connected"
          || this.verifying.get(offer.request_id) !== controller) return;
        if (verified.requestId !== offer.request_id || verified.manifestPath !== offer.manifest_path
          || verified.binding.instance_id !== recipient.instance_id
          || verified.binding.session_id !== recipient.session_id || !("recipient" in verified.binding)
          || verified.binding.recipient.pid !== recipient.recipient.pid
          || verified.binding.recipient.process_start !== recipient.recipient.process_start
          || verified.binding.recipient.user_sid !== recipient.recipient.user_sid) throw unavailable();
        if (this.draft.canAccept && this.draft.appshotCount + this.staged.size < 4) {
          accepted = this.options.consumer?.stageWindows?.(verified) ?? false;
          if (accepted) this.staged.set(offer.request_id, offer);
        }
      } catch { /* A failed/aborted native read never authorizes a draft. */ }
      finally {
        clearTimeout(timer);
        if (abortRead) controller.signal.removeEventListener("abort", abortRead);
        owned = this.verifying.get(offer.request_id) === controller;
        if (owned) this.verifying.delete(offer.request_id);
      }
      if (owned && this.socket === socket && !this.abort.signal.aborted && this.session === recipient.session_id) {
        try { acknowledge(accepted); } catch { socket.destroy(); }
      }
    })();
  }
}
