/**
 * Turn-Change Ledger · M2 (TUI summary line).
 *
 * Pure helpers that turn a `turn_changes` backend event payload into the single
 * summary line shown after a turn, plus the per-turn render gate that keeps a
 * replayed/duplicated event from printing twice.
 */

export type TurnChangesFile = {
  path: string;
  state: string;
  added: number | null;
  removed: number | null;
  compare: string;
  reason: string;
};

export type TurnChangesEventData = {
  session_id: string;
  request_id: string;
  turn_seq: number;
  files: TurnChangesFile[];
  unknown_count: number;
  totals: { files?: number; added?: number; removed?: number };
};

/** Maximum number of remembered turn keys before the oldest is evicted. */
const RENDER_GATE_LIMIT = 4096;

/** U+2212 MINUS SIGN — the frozen glyph for the removed-count column. */
const MINUS = "\u2212";

function asCount(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function asFileArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

/** Suffix appended to a non-empty summary line: the command that shows details. */
const CHANGES_HINT = " · /changes 查看";

/**
 * Format the one-line summary for a completed turn.
 *
 * Returns `null` when there is nothing to say — neither changed files nor
 * unconfirmed paths — in which case the caller must not render a line.
 * Malformed payloads degrade instead of throwing.
 */
export function formatTurnChangesSummary(event: TurnChangesEventData): string | null {
  if (event === null || typeof event !== "object") return null;
  const files = asFileArray(event.files);
  const unknownCount = asCount(event.unknown_count);
  const fileCount = files.length;

  if (fileCount > 0) {
    const totals = (event as { totals?: unknown }).totals;
    const totalsRecord =
      totals !== null && typeof totals === "object" ? (totals as Record<string, unknown>) : {};
    const added = asCount(totalsRecord.added);
    const removed = asCount(totalsRecord.removed);
    let line = `✎ 本轮 ${fileCount} 个文件 +${added} ${MINUS}${removed}`;
    if (unknownCount > 0) {
      line += ` · 另有 ${unknownCount} 个路径未能确认`;
    }
    return line + CHANGES_HINT;
  }

  if (unknownCount > 0) {
    return `✎ 另有 ${unknownCount} 个路径未能确认` + CHANGES_HINT;
  }

  return null;
}

/**
 * Per-process guard ensuring one rendered summary line per turn, even when the
 * same `turn_changes` event is re-delivered (reconnect/replay).
 */
export class TurnChangesRenderGate {
  private readonly seen = new Set<string>();

  shouldRender(event: TurnChangesEventData, currentSessionId: string): boolean {
    if (event === null || typeof event !== "object") return false;
    const sessionId = (event as { session_id?: unknown }).session_id;
    if (typeof currentSessionId === "string" && currentSessionId !== "" && sessionId !== currentSessionId) {
      return false;
    }
    const key = `${String(sessionId)}:${String((event as { request_id?: unknown }).request_id)}:${String(
      (event as { turn_seq?: unknown }).turn_seq,
    )}`;
    if (this.seen.has(key)) return false;
    this.seen.add(key);
    if (this.seen.size > RENDER_GATE_LIMIT) {
      const oldest = this.seen.values().next();
      if (!oldest.done) this.seen.delete(oldest.value);
    }
    return true;
  }
}
