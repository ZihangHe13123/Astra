/**
 * A paired phone runs the desktop's own renderer. Its `window.astra` is a bridge that sends the
 * window's calls to the desktop over the gateway and receives the window's events from it.
 */

/** Calls the desktop answers for a phone; everything else in the bridge is handled on the phone. */
export const PHONE_INVOKE_METHODS = ["bootstrap", "create", "close", "send", "query", "deleteSession", "jobs", "memory"] as const;
export type PhoneInvokeMethod = typeof PHONE_INVOKE_METHODS[number];

/** Desktop preferences a phone shows but keeps its own copy of: names and pins of saved sessions. */
export const SHARED_PREFERENCE_KEYS = ["titles", "pinned", "projects", "workspaces"] as const;

/**
 * Control lines on the event stream, besides the window's own events. Event numbers count within one
 * running gateway, named by `instance`; a phone resuming from another instance starts again.
 */
export type PhoneStreamControl = { ready: true; seq: number; instance: string } | { reload: true };

/** Where a phone stopped reading: the last event number it saw and the gateway that numbered it. */
export type PhoneStreamPosition = { seq: number; instance: string };

/** The signed text that proves a phone holds its device key. A fixed prefix keeps the signature good for nothing else. */
export function phoneAuthMessage(nonce: string, deviceId: string): string {
  return `astra-phone-auth-v1\n${nonce}\n${deviceId}`;
}

/** Pairing codes avoid letters and digits that are easy to misread on a phone keyboard. */
export const PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";
export const PAIRING_CODE_LENGTH = 8;

/** The phone settings section, as the window asks for it. */
export type PhoneDevice = { id: string; name: string; createdAt: string; lastSeen?: string };
export type PhoneStatus = {
  enabled: boolean;
  /** Listening on 127.0.0.1:port; tailscale serve puts HTTPS in front of it. */
  port: number;
  running: boolean;
  error?: string;
  /** The address the phone opens, when Tailscale reports this Mac's name. */
  url?: string;
  devices: PhoneDevice[];
  pairing?: { code: string; expiresAt: number };
};
export type PhoneAction = "status" | "enable" | "disable" | "pair" | "revoke";

/** Splits a text/event-stream buffer into complete events (id and data); comments (heartbeats) are skipped. */
export function parseEventStream(buffer: string): { events: { id?: number; data: string }[]; rest: string } {
  const blocks = buffer.replace(/\r\n/g, "\n").split("\n\n");
  const rest = blocks.pop() ?? "";
  const events: { id?: number; data: string }[] = [];
  for (const block of blocks) {
    let id: number | undefined; const data: string[] = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("id:")) id = Number(line.slice(3).trim());
      else if (line.startsWith("data:")) data.push(line.slice(line.startsWith("data: ") ? 6 : 5));
    }
    if (data.length) events.push({ ...(Number.isSafeInteger(id) ? { id } : {}), data: data.join("\n") });
  }
  return { events, rest };
}
