import React, { useState } from "react";

/** A status word as the side panel shows it; an unknown one is shown as it came. */
export function statusLabel(status?: string) { return ({ running: "运行中", active: "进行中", scheduled: "已安排", completed: "已完成", idle: "待命", pending: "等待中", in_progress: "进行中", done: "已完成", cancelled: "已取消", failed: "失败", interrupted: "已中断", paused: "已暂停", ready: "就绪", starting: "启动中", waiting: "等待中", stopped: "已停止" } as Record<string, string>)[status || ""] || status || ""; }
/** The event as it came, folded, and only rendered once opened. */
export function RawDetails({ value, label = "诊断详情" }: { value: unknown; label?: string }) {
  const [open, setOpen] = useState(false);
  return <details className="tool-detail" onToggle={e => setOpen(e.currentTarget.open)}><summary className="muted small">{label}</summary>{open && <pre>{JSON.stringify(value, null, 2)}</pre>}</details>;
}
