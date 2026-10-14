import type { TurnChangeNote } from "@astra/ui-core/session-state";

export type ChangeSource = Pick<TurnChangeNote, "session_id" | "request_id" | "turn_seq">;
export type ChangeTarget = { serial: number; source: ChangeSource };
/** A card names a durable ledger record, never its shifting position in the latest-turn list. */
export function changeSource(note: ChangeSource): ChangeSource {
  return { session_id: note.session_id, request_id: note.request_id, turn_seq: note.turn_seq };
}
