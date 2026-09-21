import { appendFile, mkdir } from "node:fs/promises";
import { join } from "node:path";
import type { ProtocolDiagnostic } from "@astra/ui-core/backend-protocol";
export * from "@astra/ui-core/backend-protocol";

export async function writeProtocolDiagnostic(root: string, diagnostic: ProtocolDiagnostic): Promise<void> {
  const directory = process.env.AGENT_LOG_DIR?.trim() || join(root, ".logs");
  await mkdir(directory, { recursive: true });
  await appendFile(join(directory, "tui-protocol.jsonl"), JSON.stringify({
    timestamp: new Date().toISOString(), ...diagnostic,
  }) + "\n", { mode: 0o600 });
}
