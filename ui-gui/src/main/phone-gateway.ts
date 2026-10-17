import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http";
import { existsSync, readFileSync, realpathSync, statSync } from "node:fs";
import { extname, resolve, sep } from "node:path";
import type { DesktopEvent } from "../bridge.js";
import { PHONE_INVOKE_METHODS, type PhoneInvokeMethod, type PhoneStreamControl } from "../phone-protocol.js";
import { PhoneAuthError, type PhoneDevices } from "./phone-devices.js";

const AUTH_BODY_LIMIT = 16 * 1024;
const INVOKE_BODY_LIMIT = 2 * 1024 * 1024;
const STREAM_BACKLOG_LIMIT = 8 * 1024 * 1024;
const REPLAY_EVENTS = 4000;
const HEARTBEAT_MS = 20_000;
const TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8", ".json": "application/json", ".webmanifest": "application/manifest+json",
  ".png": "image/png", ".svg": "image/svg+xml", ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
  ".ico": "image/x-icon", ".wasm": "application/wasm", ".bcmap": "application/octet-stream", ".pfb": "application/octet-stream",
};
// The renderer's own policy, without frames: model-written cards would share the app's origin here.
const PAGE_POLICY = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; "
  + "img-src 'self' data: blob: https:; connect-src 'self'; worker-src 'self' blob:; manifest-src 'self'; frame-src 'none'; "
  + "object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'";
const PHONE_HEAD = '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content"/>'
  + '<meta name="apple-mobile-web-app-capable" content="yes"/><meta name="apple-mobile-web-app-status-bar-style" content="default"/>'
  + '<meta name="apple-mobile-web-app-title" content="Astra"/><link rel="manifest" href="./manifest.webmanifest"/>'
  + '<link rel="apple-touch-icon" href="./phone/icon-180.png"/><script src="./phone/bridge.js"></script>';

type Stream = { deviceId: string; response: ServerResponse };
export type PhoneInvoke = (deviceId: string, method: PhoneInvokeMethod, args: unknown[]) => Promise<unknown> | unknown;
export type PhoneGatewayOptions = {
  /** The phone's own files (dist/phone): the bridge script, manifest, service worker and icons. */
  staticRoot: string;
  /** The desktop renderer (dist/renderer), which the phone runs as its interface. */
  rendererRoot: string;
  /** Answers a paired phone's calls; the desktop process owns sessions and their rules. */
  invoke: PhoneInvoke;
  port: number;
};

/**
 * Serves paired phones from the desktop process, so a phone runs the same interface on the same
 * live sessions as the window. It listens on loopback only; `tailscale serve` puts HTTPS on the
 * tailnet in front of it.
 */
export class PhoneGateway {
  private server?: Server;
  private streams = new Set<Stream>();
  private heartbeat?: ReturnType<typeof setInterval>;
  private boundPort = 0;
  private seq = 0;
  private recent: { seq: number; text: string }[] = [];
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

  /** Called with every event the window gets. Appshot state is the window's own and is not sent. */
  publish(event: DesktopEvent) {
    if ("appshot" in event) return;
    const seq = ++this.seq;
    const text = `id: ${seq}\ndata: ${JSON.stringify(event)}\n\n`;
    // Kept so a phone that briefly lost its connection gets what it missed instead of starting over.
    this.recent.push({ seq, text });
    if (this.recent.length > REPLAY_EVENTS) this.recent.splice(0, this.recent.length - REPLAY_EVENTS);
    for (const stream of this.streams) this.write(stream, text);
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
        return await this.api(url, request, response);
      }
      if (request.method !== "GET" && request.method !== "HEAD") return send(response, 405, { error: "Method not allowed" });
      return this.file(url.pathname, response);
    } catch (error) {
      if (!response.headersSent) send(response, error instanceof PhoneAuthError ? 403 : 400, { error: error instanceof Error ? error.message : String(error) });
      else response.destroy();
    }
  }

  private async api(url: URL, request: IncomingMessage, response: ServerResponse) {
    const path = url.pathname;
    if (path === "/api/events" && request.method === "GET") return this.events(url, request, response);
    if (request.method !== "POST") return send(response, 405, { error: "Method not allowed" });
    if (path === "/api/invoke") {
      const deviceId = this.bearer(request);
      if (!deviceId) return send(response, 401, { error: "Sign in again" });
      const body = await readJson(request, INVOKE_BODY_LIMIT);
      const method = body.method as PhoneInvokeMethod;
      if (!PHONE_INVOKE_METHODS.includes(method)) return send(response, 400, { error: "Unsupported call" });
      const args = Array.isArray(body.args) ? body.args : [];
      try { return send(response, 200, { result: (await this.options.invoke(deviceId, method, args)) ?? null }); }
      catch (error) { return send(response, 400, { error: error instanceof Error ? error.message : String(error) }); }
    }
    const body = await readJson(request, AUTH_BODY_LIMIT);
    if (path === "/api/pair") {
      const device = this.devices.pair(body.code, body.name, body.publicKey);
      return send(response, 200, { deviceId: device.id, name: device.name });
    }
    if (path === "/api/challenge") return send(response, 200, { nonce: this.devices.challenge(body.deviceId) });
    if (path === "/api/session") return send(response, 200, this.devices.signIn(body.deviceId, body.nonce, body.signature));
    return send(response, 404, { error: "Not found" });
  }

  private bearer(request: IncomingMessage): string | undefined {
    const token = /^Bearer ([A-Za-z0-9_-]{20,})$/.exec(request.headers.authorization || "")?.[1];
    return token ? this.devices.deviceForToken(token) : undefined;
  }

  private events(url: URL, request: IncomingMessage, response: ServerResponse) {
    const deviceId = this.bearer(request);
    if (!deviceId) return send(response, 401, { error: "Sign in again" });
    response.writeHead(200, { "Content-Type": "text/event-stream; charset=utf-8", "Cache-Control": "no-store",
      Connection: "keep-alive", "X-Accel-Buffering": "no" });
    const stream: Stream = { deviceId, response };
    // The response's close is the connection ending; a request's close can come as soon as its (empty) body is read.
    response.on("close", () => this.streams.delete(stream));
    const after = url.searchParams.has("after") ? Number(url.searchParams.get("after")) : undefined;
    if (after !== undefined && Number.isSafeInteger(after)) {
      const missed = this.recent.filter(item => item.seq > after);
      const complete = after >= this.seq || (missed.length > 0 && missed[0].seq === after + 1);
      if (!complete) { this.write(stream, control({ reload: true })); response.end(); return; }
      for (const item of missed) this.write(stream, item.text);
    }
    this.streams.add(stream);
    this.write(stream, control({ ready: true, seq: this.seq }));
  }

  private write(stream: Stream, text: string) {
    // A phone that stopped reading is dropped; it reconnects and catches up or reloads.
    if (stream.response.writableLength > STREAM_BACKLOG_LIMIT) { this.close(stream); return; }
    stream.response.write(text);
  }

  private close(stream: Stream) {
    this.streams.delete(stream);
    stream.response.end();
  }

  private file(pathname: string, response: ServerResponse) {
    if (pathname === "/" || pathname === "/index.html") return this.page(response);
    // The phone's own files live under /phone; the manifest and service worker are served at the root for their scope.
    const own = pathname === "/sw.js" || pathname === "/manifest.webmanifest";
    const root = realpathSync(own || pathname.startsWith("/phone/") ? this.options.staticRoot : this.options.rendererRoot);
    const relative = decodeURIComponent(own ? pathname : pathname.startsWith("/phone/") ? pathname.slice("/phone".length) : pathname).replace(/^\/+/, "");
    const path = resolve(root, relative);
    if (!path.startsWith(root + sep) || !existsSync(path) || !statSync(path).isFile() || !realpathSync(path).startsWith(root + sep)) {
      return send(response, 404, { error: "Not found" });
    }
    const type = TYPES[extname(path)] || "application/octet-stream";
    response.writeHead(200, { "Content-Type": type,
      "Cache-Control": relative.startsWith("assets/") ? "public, max-age=31536000, immutable" : "no-cache" });
    response.end(readFileSync(path));
  }

  /** The renderer's page with the phone's bridge loaded first, so `window.astra` exists before the app starts. */
  private page(response: ServerResponse) {
    const html = readFileSync(resolve(this.options.rendererRoot, "index.html"), "utf8")
      .replace(/<meta name="viewport"[^>]*>/, "")
      .replace("<head>", `<head>${PHONE_HEAD}`);
    response.writeHead(200, { "Content-Type": TYPES[".html"], "Cache-Control": "no-cache", "Content-Security-Policy": PAGE_POLICY });
    response.end(html);
  }
}

function control(message: PhoneStreamControl) { return `data: ${JSON.stringify(message)}\n\n`; }

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

async function readJson(request: IncomingMessage, limit: number): Promise<Record<string, unknown>> {
  let size = 0; const chunks: Buffer[] = [];
  for await (const chunk of request) {
    size += chunk.length;
    if (size > limit) throw new Error("Request too large");
    chunks.push(chunk);
  }
  const value = JSON.parse(Buffer.concat(chunks).toString("utf8") || "{}");
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Expected a JSON object");
  return value;
}
