import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { createInterface } from "node:readline";
import { randomUUID } from "node:crypto";

type Pending = { resolve: (value: any) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> };

/** One read-only query process per desktop host; no model/backend initialization. */
export class QueryClient {
  private child?: ChildProcessWithoutNullStreams;
  private pending = new Map<string, Pending>();
  private commands?: Promise<any>;
  private closed = false;
  private closing?: Promise<void>;
  private retired = new Set<Promise<void>>();

  constructor(readonly root: string, readonly python: string, readonly timeoutMs = 30_000) {}

  private fail(error: Error) {
    for (const pending of this.pending.values()) { clearTimeout(pending.timer); pending.reject(error); }
    this.pending.clear();
  }

  private recycle(child: ChildProcessWithoutNullStreams, error: Error) {
    if (this.child !== child) return;
    // Detach first so an immediate retry cannot queue behind a stuck request.
    // This worker owns disposable read caches only; SQLite rolls back on exit.
    this.child = undefined;
    this.fail(error);
    const reaped = new Promise<void>(resolve => child.once("close", () => resolve()));
    this.retired.add(reaped);
    void reaped.then(() => this.retired.delete(reaped));
    child.kill("SIGKILL");
    child.stdin.destroy();
  }

  private worker() {
    if (this.child) return this.child;
    const child = spawn(this.python, ["-m", "agent.ui.queries", "--serve"], {
      cwd: this.root, env: { ...process.env, PYTHONUNBUFFERED: "1", PYTHONIOENCODING: "utf-8" }, windowsHide: true,
    });
    this.child = child;
    child.stderr.resume();
    const lines = createInterface({ input: child.stdout.setEncoding("utf8") });
    lines.on("line", line => {
      if (this.child !== child) return;
      try {
        const response = JSON.parse(line);
        const request = this.pending.get(response.id);
        if (!request) return;
        clearTimeout(request.timer); this.pending.delete(response.id);
        response.ok ? request.resolve(response.result) : request.reject(new Error(response.error || "Local query failed"));
      } catch {
        this.recycle(child, new Error("Invalid local query response"));
      }
    });
    // EPIPE and spawn failures are receipts of failure, not unhandled events.
    child.stdin.on("error", error => this.recycle(child, error));
    child.on("error", error => this.recycle(child, error));
    child.on("close", () => {
      lines.close();
      if (this.child === child) {
        this.child = undefined;
        this.fail(new Error(this.closed ? "Local query service closed" : "Local query service disconnected"));
      }
    });
    return child;
  }

  query(method: string, params: Record<string, unknown> = {}): Promise<any> {
    if (this.closed) return Promise.reject(new Error("Local query service closed"));
    if (method === "commands" && this.commands) return this.commands;
    const request = new Promise((resolve, reject) => {
      const id = randomUUID();
      const child = this.worker();
      const timer = setTimeout(() => {
        this.recycle(child, new Error("Local query timed out"));
      }, this.timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      try { child.stdin.write(JSON.stringify({ id, method, params }) + "\n"); }
      catch (error) { clearTimeout(timer); this.pending.delete(id); reject(error); }
    });
    if (method === "commands") {
      this.commands = request;
      void request.catch(() => { if (this.commands === request) this.commands = undefined; });
    }
    return request;
  }

  close(): Promise<void> {
    if (this.closing) return this.closing;
    this.closed = true;
    this.fail(new Error("Local query service closed"));
    const child = this.child;
    const ending = [...this.retired];
    if (child) ending.push(new Promise<void>(resolve => {
      const terminate = setTimeout(() => child.kill(), 2_000);
      const force = setTimeout(() => child.kill("SIGKILL"), 4_000);
      child.once("close", () => { clearTimeout(terminate); clearTimeout(force); resolve(); });
      child.stdin.end();
    }));
    this.closing = Promise.all(ending).then(() => {});
    return this.closing;
  }
}
