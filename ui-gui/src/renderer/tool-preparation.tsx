import React from "react";
import type { SessionState } from "@astra/ui-core/session-state";

/** Model output in progress is not an admitted or executed tool call. */
export function ToolPreparation({ preparation }: { preparation: SessionState["preparation"] }) {
  if (!preparation || preparation.state !== "preparing" || !preparation.calls.length) return null;
  return <section className="tool-preparation" aria-label="工具参数准备进度">
    <div className="small muted">正在准备工具参数 · 尚未执行</div>
    {preparation.calls.map(call => <div className="preparing-call" key={`${preparation.attempt_id}:${call.index}`}>
      <strong>{call.name || "工具"}</strong><span className="small muted">{Math.max(0, call.argument_chars).toLocaleString()} 字符</span>
      {call.summary && <p>{call.summary}</p>}
    </div>)}
  </section>;
}
