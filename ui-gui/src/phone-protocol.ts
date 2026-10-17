import type { SessionState, UIEvent } from "@astra/ui-core/session-state";

/** What a paired phone receives on its event stream. The desktop's own events, minus what only the window uses. */
export type PhoneStreamEvent =
  | { type: "hello"; sessions: SessionState[]; active: string }
  | { type: "event"; runtime: string; revision: number; event: UIEvent }
  | { type: "removed"; runtime: string }
  | { type: "refresh" };

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
