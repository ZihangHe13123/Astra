import { spawn } from "node:child_process";

/** Only named session actions cross this boundary; output paths stay in the host. */
export function sessionAction(root: string, python: string, action: "export" | "delete", name: string, mode: string): Promise<any> {
  return new Promise((resolve, reject) => {
    const child = spawn(python, ["-m", "agent.ui.session_actions"], { cwd: root, env: process.env, windowsHide: true });
    let output = "";
    let oversized = false;
    const timer = setTimeout(() => { child.kill(); reject(new Error("会话操作超时，请先确认结果。")); }, 30000);
    child.stdout.setEncoding("utf8");
    child.stdout.on("data", chunk => { output += chunk; if (output.length > 64 * 1024 * 1024) { oversized = true; child.kill(); } });
    child.stderr.resume();
    child.on("error", error => { clearTimeout(timer); reject(error); });
    child.on("close", () => {
      clearTimeout(timer);
      if (oversized) { reject(new Error("会话过大，无法在桌面内导出。")); return; }
      try { const reply = JSON.parse(output); reply.ok ? resolve(reply.result) : reject(new Error(reply.error)); }
      catch { reject(new Error("会话操作没有返回有效结果，请刷新列表核对。")); }
    });
    child.stdin.end(JSON.stringify({ action, name, mode }) + "\n");
  });
}
