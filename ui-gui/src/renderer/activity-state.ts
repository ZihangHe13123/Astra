/** What the conversation shows of the agent's work: each tool call as one step, in the place it happened. */
import type { Message, TurnChangeNote, UIEvent } from "@astra/ui-core/session-state";

export { describeTool, span, type Step } from "@astra/ui-core/tool-activity";

export type Row = Message & { tools?: UIEvent[]; changes?: TurnChangeNote };
/** The rows of a conversation with the agent's work in it: after each message, the tool calls made
 * then, and the file changes reported then. A tool result with no place (a restored history) is
 * returned in `unplaced`, and so is one whose message is not among these rows. */
export function withActivity(rows: Message[], tools: UIEvent[] = [], changes: TurnChangeNote[] = []): { rows: Row[]; unplaced: UIEvent[] } {
  if (!tools.length && !changes.length) return { rows, unplaced: [] };
  const known = new Set(rows.map(row => row.id));
  const here = (place: unknown): place is string => typeof place === "string" && (place === "" || known.has(place));
  const toolsAfter = new Map<string, UIEvent[]>(), changesAfter = new Map<string, TurnChangeNote[]>(), unplaced: UIEvent[] = [];
  for (const tool of tools) {
    if (!here(tool.after_message)) { unplaced.push(tool); continue; }
    toolsAfter.set(tool.after_message, [...(toolsAfter.get(tool.after_message) || []), tool]);
  }
  for (const note of changes) if (here(note.after_message)) changesAfter.set(note.after_message, [...(changesAfter.get(note.after_message) || []), note]);
  const result: Row[] = [];
  const add = (place: string) => {
    const made = toolsAfter.get(place);
    if (made) result.push({ id: `activity:${place}`, role: "activity", content: "", tools: made });
    for (const note of changesAfter.get(place) || []) result.push({ id: `changes:${note.request_id}:${note.turn_seq}`, role: "changes", content: "", changes: note });
  };
  add("");
  for (const row of rows) { result.push(row); add(row.id); }
  return { rows: result, unplaced };
}
