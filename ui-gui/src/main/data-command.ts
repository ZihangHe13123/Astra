import { execFile } from "node:child_process";

type Listed = { key: string; label: string };
/** What `astra data wipe` answers once it got past its refusals: what went, and what could not and why. */
export type WipeAnswer = { removed: (Listed & { files: number })[]; failed: (Listed & { error: string })[]; kept: string[] };

/** The command ran but did not do all of it; `result` is its answer, for a wipe what went and what failed. */
export class DataCommandIncomplete extends Error {
  constructor(message: string, readonly result: WipeAnswer) { super(message); this.name = "DataCommandIncomplete"; }
}

/**
 * One run of `astra data` (agent/cli/data_commands.py); it answers with a JSON line. A refusal (`error`) rejects
 * with its reason. Items that could not be deleted, or a failing exit, reject as incomplete: never as success.
 */
export function dataCommand(python: string, root: string, args: string[], timeout: number, env: NodeJS.ProcessEnv = process.env): Promise<any> {
  return new Promise((done, fail) => execFile(python, ["-m", "agent.cli.data_commands", ...args],
    { cwd: root, env, timeout, windowsHide: true, encoding: "utf8", maxBuffer: 16 * 1024 * 1024 },
    (error, stdout) => {
      let result: any;
      try { result = JSON.parse(String(stdout).trim().split("\n").at(-1) || "null"); } catch { /* reported below */ }
      if (result?.error) fail(new Error(result.error));
      else if (!result || typeof result !== "object") fail(new Error(error?.message || "没有得到结果。"));
      else if (Array.isArray(result.failed) && result.failed.length) fail(new DataCommandIncomplete(incomplete(result.failed), result));
      else if (error) fail(new DataCommandIncomplete(`没有做完：${error.message}`, { removed: [], failed: [], kept: [], ...result }));
      else done(result);
    }));
}

/** The person's own words for a wipe that left something: each item that stayed, with the reason it gave. */
function incomplete(failed: WipeAnswer["failed"]) {
  const items = failed.map(item => `${item.label || item.key}（${item.error}）`).join("；");
  return `没有全部清除，这些还在：${items}。其余的已删除；关掉占用这些文件的程序后，可以再清除一次。`;
}
