import type { SessionState, UIEvent } from "@astra/ui-core/session-state";
import type { FilePreview } from "./file-preview-types.js";

export type SessionEntry = { name: string; mode: string; modified: number };
export type Preferences = { theme: string; drafts: Record<string, string>; attachments: Record<string, string[]>; workspaces: Record<string, string>; pinned: string[]; projects: string[]; titles: Record<string, string>; timeline: boolean; detailWidth?: number; lastSession?: { session: string; mode: string; workspace: string } | null };
export type DesktopEvent = { removed: string } | { runtime: string; revision: number; event: UIEvent } | { refresh: true } | { appshot: { connection: string; enabled: boolean } };
export type Bootstrap = { sessions: SessionState[]; active: string; workspace: string; preferences: Preferences; appshot: { connection: string; enabled: boolean } };
export type CommandDescription = { id: string; command: string; description: string; takes_args: boolean; group: string; options: { command: string; description: string; completion: string; submitValue?: string }[] };
import type { PhoneAction, PhoneStatus } from "./phone-protocol.js";
export interface DesktopBridge {
  /** Set on a paired phone, where the window runs over the gateway and Mac-only actions are unavailable. */
  remote?: boolean;
  bootstrap(): Promise<Bootstrap>;
  /** Paired phones (settings): their list, pairing and removal, and whether the gateway runs. */
  phone(action: PhoneAction, value?: string): Promise<PhoneStatus>;
  create(options: { session?: string; mode?: string; workspace?: string }): Promise<SessionState>;
  sessionAction(action: "export" | "delete", entry: { name: string; mode: string }): Promise<{ cancelled?: boolean; path?: string; deleted?: boolean }>;
  select(id: string): Promise<void>;
  close(id: string): Promise<void>;
  send(id: string, command: UIEvent): Promise<void>;
  query(method: string, params?: Record<string, unknown>, runtime?: string): Promise<any>;
  /** The person's change to a scheduled job (pause, resume, remove, run, read) or to the quiet hours. */
  jobs(action: "pause" | "resume" | "remove" | "run" | "read" | "quiet", value?: string, extra?: string): Promise<any>;
  /** The person's change to their memory: core entries (add, edit, remove), saved memories (forget, correct), proposals. */
  memory(action: "core-add" | "core-edit" | "core-remove" | "forget" | "correct" | "accept" | "dismiss", ...values: string[]): Promise<string>;
  preferences(value: Partial<Preferences>): Promise<Preferences>;
  flushPreferences(value: Partial<Preferences>): void;
  choose(kind: "files" | "folder"): Promise<string[]>;
  clipboardImage(): Promise<string | null>;
  copyText(text: string): Promise<void>;
  appshot(id: string, action: "command" | "remove", value: string): Promise<void>;
  droppedPaths(files: File[]): string[];
  openExternal(url: string): Promise<void>;
  file(id: string, path: string, action: "preview" | "open" | "reveal" | "version" | "snapshot", requestId?: string): Promise<FilePreview>;
  cancelPreview(id: string, requestId: string): Promise<void>;
  onEvents(handler: (events: DesktopEvent[]) => void): () => void;
}
declare global { interface Window { astra: DesktopBridge; } }
