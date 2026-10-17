import { projectEvent, textContent, type SessionState } from "@astra/ui-core/session-state";
import type { PhoneStreamEvent } from "../phone-protocol.js";

/** The sessions open on the desktop, as the phone follows them. */
export type PhoneSessions = {
  sessions: Record<string, SessionState>;
  order: string[];
  active: string;
  /** An event for a session this phone has no snapshot of: the stream is reopened for a fresh one. */
  stale: boolean;
};

export const emptySessions: PhoneSessions = { sessions: {}, order: [], active: "", stale: false };

/** Applies one stream message the way the desktop window does: a snapshot, then only newer revisions. */
export function applyStream(current: PhoneSessions, message: PhoneStreamEvent): PhoneSessions {
  if (message.type === "hello") {
    return { sessions: Object.fromEntries(message.sessions.map(session => [session.id, session])),
      order: message.sessions.map(session => session.id), active: message.active, stale: false };
  }
  if (message.type === "removed") {
    if (!current.sessions[message.runtime]) return current;
    const { [message.runtime]: _removed, ...sessions } = current.sessions;
    return { ...current, sessions, order: current.order.filter(id => id !== message.runtime),
      active: current.active === message.runtime ? "" : current.active };
  }
  if (message.type === "event") {
    const session = current.sessions[message.runtime];
    if (!session) return { ...current, stale: true };
    if (message.revision <= session.revision) return current;
    return { ...current, sessions: { ...current.sessions,
      [message.runtime]: { ...projectEvent(session, message.event), revision: message.revision } } };
  }
  return current;
}

/** Splits a text/event-stream buffer into complete data payloads; comments (heartbeats) are skipped. */
export function parseEventStream(buffer: string): { payloads: string[]; rest: string } {
  const blocks = buffer.replace(/\r\n/g, "\n").split("\n\n");
  const rest = blocks.pop() ?? "";
  const payloads = blocks.map(block => block.split("\n").filter(line => line.startsWith("data:"))
    .map(line => line.slice(line.startsWith("data: ") ? 6 : 5)).join("\n")).filter(Boolean);
  return { payloads, rest };
}

/** A session's name on the phone: the window's own title is a desktop preference, so the first request stands in. */
export function sessionTitle(session: SessionState): string {
  const first = session.messages.find(message => message.role === "user");
  const text = first ? textContent(first.content).replace(/\s+/g, " ").trim() : "";
  if (text) return text.length > 40 ? `${text.slice(0, 39)}…` : text;
  return session.isDraft ? "新会话" : session.session || "会话";
}
