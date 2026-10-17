import { execFile } from "node:child_process";

/** A desktop app started from Finder does not inherit the shell's PATH, so the usual install places are tried in turn. */
const CANDIDATES = [
  "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
  "/opt/homebrew/bin/tailscale",
  "/usr/local/bin/tailscale",
  "tailscale",
];

type Run = (file: string, args: string[]) => Promise<string>;
const run: Run = (file, args) => new Promise((resolve, reject) => {
  execFile(file, args, { timeout: 3000, windowsHide: true, maxBuffer: 4 * 1024 * 1024 }, (error, stdout) => error ? reject(error) : resolve(stdout));
});

/** This machine's MagicDNS name (e.g. "mac.tail1234.ts.net"), or undefined when Tailscale is absent or not connected. */
export async function tailscaleName(execute: Run = run): Promise<string | undefined> {
  for (const file of CANDIDATES) {
    try {
      const status = JSON.parse(await execute(file, ["status", "--json"]));
      const name = typeof status?.Self?.DNSName === "string" ? status.Self.DNSName.replace(/\.$/, "") : "";
      return /^[a-z0-9-]+(\.[a-z0-9-]+)*\.ts\.net$/i.test(name) ? name.toLowerCase() : undefined;
    } catch { /* try the next place */ }
  }
  return undefined;
}
