import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { existsSync, readFileSync, realpathSync, statSync } from "node:fs";
import { extname, resolve, sep } from "node:path";
import type { SessionState } from "@astra/ui-core/session-state";
import type { DesktopEvent } from "../bridge.js";
import type { PhoneStreamEvent } from "../phone-protocol.js";
import { PhoneAuthError, type PhoneDevices } from "./phone-devices.js";

const BODY_LIMIT = 16 * 1024;
const STREAM_BACKLOG_LIMIT = 8 * 1024 * 1024;
const HEARTBEAT_MS = 20_000;
const TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".json": "application/json", ".webmanifest": "application/manifest+json", ".png": "image/png", ".svg": "image/svg+xml",
  ".woff2": "font/woff2", ".ico": "image/x-icon",
};
const PAGE_POLICY = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
  + "connect-src 'self'; manifest-src 'self'; worker-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";

type Stream = { deviceId: string; response: ServerResponse };
export type PhoneGatewayOptions = {
  /** The built phone app (dist/phone). */
  staticRoot: string;
  /** The sessions open in this desktop, as the window sees them. */
  snapshot: () => { sessions: SessionState[]; active: string };
  port: number;
};

/**
 * Serves paired phones from the desktop process, so a phone sees the same live sessions as the
 * window. It listens on loopback only; `tailscale serve` puts HTTPS on the tailnet in front of it.
 * Phase 1 is read-only: a phone can pair, sign in and follow events, and send nothing to a session.
 */
export class PhoneGateway {
  private server?: Server;
  private streams = new Set<Stream>();
  private heartbeat?: ReturnType<typeof setInterval>;
  private boundPort = 0;
  private stopRevoked: () => void;

  constructor(private readonly devices: PhoneDevices, private readonly options: PhoneGatewayOptions) {
    this.stopRevoked = devices.onRevoked(deviceId => {
      for (const stream of [...this.streams]) if (stream.deviceId === deviceId) this.close(stream);
    });
  }

  get running() { return !!this.server?.listening; }
  get port() { return this.boundPort || this.options.port; }

  async start(): Promise<number> {
    if (this.server?.listening) return this.boundPort;
    const server = createServer((request, response) => { void this.handle(request, response); });
    await new Promise<void>((done, fail) => {
      server.once("error", fail);
      server.listen(this.options.port, "127.0.0.1", () => { server.off("error", fail); done(); });
    });
    this.server = server;
    const address = server.address();
    this.boundPort = typeof address === "object" && address ? address.port : this.options.port;
    this.heartbeat = setInterval(() => { for (const stream of this.streams) this.write(stream, ": ping\n\n"); }, HEARTBEAT_MS);
    return this.boundPort;
  }

  async stop() {
    clearInterval(this.heartbeat); this.heartbeat = undefined;
    for (const stream of [...this.streams]) this.close(stream);
    const server = this.server; this.server = undefined; this.boundPort = 0;
    if (server) await new Promise<void>(done => server.close(() => done()));
  }

  dispose() { this.stopRevoked(); return this.stop(); }

  /** Called with every event the window gets. Window-only events (Appshot state) are not sent. */
  publish(event: DesktopEvent) {
    if (!this.streams.size) return;
    let message: PhoneStreamEvent | undefined;
    if ("runtime" in event) message = { type: "event", runtime: event.runtime, revision: event.revision, event: event.event };
    else if ("removed" in event) message = { type: "removed", runtime: event.removed };
    else if ("refresh" in event) message = { type: "refresh" };
    if (!message) return;
    const line = `data: ${JSON.stringify(message)}\n\n`;
    for (const stream of this.streams) this.write(stream, line);
  }

  private async handle(request: IncomingMessage, response: ServerResponse) {
    try {
      response.setHeader("X-Content-Type-Options", "nosniff");
      response.setHeader("Referrer-Policy", "no-referrer");
      // A browser page on another site could resolve a name to 127.0.0.1; only these hosts are ours.
      if (!allowedHost(request.headers.host)) return send(response, 421, { error: "Unknown host" });
      const url = new URL(request.url || "/", "http://phone.invalid");
      if (url.pathname.startsWith("/api/")) {
        if (!sameOrigin(request)) return send(response, 403, { error: "Cross-origin request" });
        return await this.api(url.pathname, request, response);
      }
      if (request.method !== "GET" && request.method !== "HEAD") return send(response, 405, { error: "Method not allowed" });
      return this.file(url.pathname, response);
    } catch (error) {
      if (!response.headersSent) send(response, error instanceof PhoneAuthError ? 403 : 400, { error: error instanceof Error ? error.message : String(error) });
      else response.destroy();
    }
  }

  private async api(path: string, request: IncomingMessage, response: ServerResponse) {
    if (path === "/api/events" && request.method === "GET") return this.events(request, response);
    if (request.method !== "POST") return send(response, 405, { error: "Method not allowed" });
    const body = await readJson(request);
    if (path === "/api/pair") {
      const device = this.devices.pair(body.code, body.name, body.publicKey);
      return send(response, 200, { deviceId: device.id, name: device.name });
    }
    if (path === "/api/challenge") return send(response, 200, { nonce: this.devices.challenge(body.deviceId) });
    if (path === "/api/session") return send(response, 200, this.devices.signIn(body.deviceId, body.nonce, body.signature));
    return send(response, 404, { error: "Not found" });
  }

  private events(request: IncomingMessage, response: ServerResponse) {
    const token = /^Bearer ([A-Za-z0-9_-]{20,})$/.exec(request.headers.authorization || "")?.[1];
    const deviceId = token ? this.devices.deviceForToken(token) : undefined;
    if (!deviceId) return send(response, 401, { error: "Sign in again" });
    response.writeHead(200, { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-store",
      Connection: "keep-alive", "X-Accel-Buffering": "no" });
    const stream: Stream = { deviceId, response };
    this.streams.add(stream);
    // The response's close is the connection ending; a request's close can come as soon as its (empty) body is read.
    response.on("close", () => this.streams.delete(stream));
    const { sessions, active } = this.options.snapshot();
    this.write(stream, `data: ${JSON.stringify({ type: "hello", sessions, active } satisfies PhoneStreamEvent)}\n\n`);
  }

  private write(stream: Stream, text: string) {
    // A phone that stopped reading is dropped; it reconnects and starts from a fresh snapshot.
    if (stream.response.writableLength > STREAM_BACKLOG_LIMIT) { this.close(stream); return; }
    stream.response.write(text);
  }

  private close(stream: Stream) {
    this.streams.delete(stream);
    stream.response.end();
  }

  private file(pathname: string, response: ServerResponse) {
    const root = realpathSync(this.options.staticRoot);
    const relative = pathname === "/" ? "phone.html" : decodeURIComponent(pathname).replace(/^\/+/, "");
    const path = resolve(root, relative);
    if (!(path === root || path.startsWith(root + sep)) || !existsSync(path) || !statSync(path).isFile()
        || !realpathSync(path).startsWith(root + sep)) return send(response, 404, { error: "Not found" });
    const type = TYPES[extname(path)] || "application/octet-stream";
    const hashed = relative.startsWith("assets/");
    response.writeHead(200, { "Content-Type": type,
      "Cache-Control": hashed ? "public, max-age=31536000, immutable" : "no-cache",
      ...(type.startsWith("text/html") ? { "Content-Security-Policy": PAGE_POLICY } : {}) });
    response.end(readFileSync(path));
  }
}

function allowedHost(header: string | undefined): boolean {
  if (!header) return false;
  const host = header.replace(/:\d+$/, "").toLowerCase();
  return host === "127.0.0.1" || host === "localhost" || host === "[::1]" || /^[a-z0-9-]+(\.[a-z0-9-]+)*\.ts\.net$/.test(host);
}

/** Same-origin fetches may omit Origin; one that is sent must be this host. */
function sameOrigin(request: IncomingMessage): boolean {
  const origin = request.headers.origin;
  if (!origin) return true;
  try { return new URL(origin).host === request.headers.host; } catch { return false; }
}

function send(response: ServerResponse, status: number, body: unknown) {
  response.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
  response.end(JSON.stringify(body));
}

async function readJson(request: IncomingMessage): Promise<Record<string, unknown>> {
  let size = 0; const chunks: Buffer[] = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > BODY_LIMIT) throw new Error("Request too large");
    chunks.push(chunk);
  }
  const value = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Expected a JSON object");
  return value;
}
