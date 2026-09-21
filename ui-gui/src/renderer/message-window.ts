import type { Message } from "@astra/ui-core/session-state";
export function messageOffsets(messages: Pick<Message, "id" | "content">[], measured: Map<string, number>): number[] {
  const result = [0];
  for (const message of messages) result.push(result[result.length - 1] + (measured.get(message.id) ?? Math.min(1200, 110 + Math.ceil(message.content.length / 80) * 24)));
  return result;
}
export function windowRange(offsets: number[], top: number, height: number, overscan = 800) {
  const count = offsets.length - 1;
  const find = (position: number) => {
    let low = 0, high = count;
    while (low < high) { const mid = (low + high) >>> 1; if (offsets[mid + 1] <= position) low = mid + 1; else high = mid; }
    return Math.max(0, Math.min(count - 1, low));
  };
  if (!count) return { start: 0, end: 0 };
  const start = find(Math.max(0, top - overscan));
  return { start, end: Math.min(count, Math.max(start + 1, find(top + height + overscan) + 1), start + 100) };
}
