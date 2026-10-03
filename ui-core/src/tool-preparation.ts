/** Ephemeral presentation only. These snapshots never authorize a tool call. */
export type PreparingCall = { index: number; call_id: string; name: string; argument_chars: number; summary: string };
export type ToolPreparation = { attempt_id: string; request_id?: string; state: "preparing"; calls: PreparingCall[] };

const clean = (value: unknown, length: number) => typeof value === "string"
  ? value.replace(/[\u0000-\u001f\u007f-\u009f]/g, " ").slice(0, length) : "";

export function reduceToolPreparation(current: ToolPreparation | undefined, event: Record<string, any>): ToolPreparation | undefined {
  if (event.replayed) return current;
  if (["history", "gui_disconnected", "backend_hello", "task_started"].includes(event.type)) return undefined;
  if (event.type === "tool_calls" || event.type === "done" || event.type === "error" && !event.recoverable) {
    return event.request_id && current?.request_id && event.request_id !== current.request_id ? current : undefined;
  }
  if (event.type !== "tool_preparing" || typeof event.attempt_id !== "string" || !event.attempt_id || event.attempt_id.length > 200) return current;
  if (event.state === "finished" || event.state === "discarded") return current?.attempt_id === event.attempt_id ? undefined : current;
  if (event.state !== "preparing" || !Array.isArray(event.calls)) return current;
  const calls: PreparingCall[] = event.calls.slice(0, 8).filter((call: any) => call && Number.isSafeInteger(call.index)
    && call.index >= 0 && Number.isSafeInteger(call.argument_chars) && call.argument_chars >= 0)
    .map((call: any) => ({ index: call.index, call_id: clean(call.call_id, 200), name: clean(call.name, 100),
      argument_chars: call.argument_chars, summary: clean(call.summary, 160) }));
  if (!calls.length) return current;
  return { attempt_id: event.attempt_id, request_id: clean(event.request_id, 200) || undefined, state: "preparing", calls };
}

export function preparationLabel(preparation?: ToolPreparation): string | undefined {
  const call = preparation?.calls[0];
  if (!call) return undefined;
  return `准备 ${call.name || "工具参数"}${call.summary ? ` · ${call.summary}` : ""} · ${call.argument_chars.toLocaleString()} 字符${preparation!.calls.length > 1 ? ` · ${preparation!.calls.length} 项` : ""}（尚未执行）`;
}
