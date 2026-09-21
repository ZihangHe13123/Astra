import React, { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { MoreHorizontal, Pin, Pencil, Download, Trash2, X } from "lucide-react";
import { Modal } from "./controls.js";
export type SessionAction = "rename" | "pin" | "export" | "close" | "delete";

export function SessionMenu({ title, pinned, live, managed = true, run }: { title: string; pinned: boolean; live: boolean; managed?: boolean; run: (action: SessionAction) => void }) {
  const [open, setOpen] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const menu = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ top: 0, left: 0 });
  const trigger = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => { if (!container.current?.contains(event.target as Node) && !menu.current?.contains(event.target as Node)) setOpen(false); };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") { event.stopPropagation(); setOpen(false); trigger.current?.focus(); } };
    document.addEventListener("pointerdown", outside); document.addEventListener("keydown", escape, true);
    menu.current?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus();
    return () => { document.removeEventListener("pointerdown", outside); document.removeEventListener("keydown", escape, true); };
  }, [open]);
  const items: { id: SessionAction; text: string; icon: typeof Pin }[] = [
    { id: "rename", text: "重命名", icon: Pencil }, { id: "pin", text: pinned ? "取消置顶" : "置顶", icon: Pin },
    ...(managed ? [{ id: "export" as const, text: "导出 Markdown", icon: Download }] : []),
    ...(live ? [{ id: "close" as const, text: "关闭会话后端", icon: X }] : []),
    ...(managed ? [{ id: "delete" as const, text: "删除会话", icon: Trash2 }] : []),
  ];
  return <div className="session-menu" ref={container}>
    <button className="icon session-more" ref={trigger} aria-label={`会话操作：${title}`} aria-haspopup="menu" aria-expanded={open} onClick={() => {
      const rect = trigger.current!.getBoundingClientRect();
      setPosition({ top: Math.max(8, Math.min(rect.bottom + 5, window.innerHeight - 215)), left: Math.max(8, Math.min(rect.right - 190, window.innerWidth - 198)) });
      setOpen(!open);
    }}><MoreHorizontal size={15}/></button>
    {open && createPortal(<div ref={menu} style={position} role="menu" aria-label="会话操作" className="session-popover" onKeyDown={event => {
      const options = [...(menu.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') || [])];
      const index = options.indexOf(document.activeElement as HTMLButtonElement);
      if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault(); const next = event.key === "Home" ? 0 : event.key === "End" ? options.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
        options[next]?.focus();
      }
    }}>{items.map(item => <button role="menuitem" key={item.id} className={item.id === "delete" ? "danger" : ""} onClick={() => { setOpen(false); run(item.id); }}><item.icon size={14}/>{item.text}</button>)}</div>, document.body)}
  </div>;
}

export function SessionActionDialog({ action, title, close, confirm }: { action: "rename" | "delete"; title: string; close: () => void; confirm: (title: string) => Promise<void> }) {
  const [name, setName] = useState(title);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  return <Modal title={action === "rename" ? "重命名会话" : "删除会话"} close={() => { if (!pending) close(); }}>
    <form className="form" onSubmit={event => {
      event.preventDefault(); if (pending) return; setPending(true); setError("");
      void confirm(name.trim()).then(close).catch(reason => setError(reason instanceof Error ? reason.message : String(reason))).finally(() => setPending(false));
    }}>
      {action === "rename" ? <label>会话名称<input value={name} onChange={event => setName(event.target.value)} maxLength={200} autoFocus/></label>
        : <p>删除“{title}”的会话历史和附件。已修改的项目文件会保留；运行中的任务需先停止。</p>}
      {error && <p className="error-text" role="alert">{error}</p>}
      <div className="actions"><button type="button" disabled={pending} onClick={close}>取消</button><button className={action === "delete" ? "danger" : "primary"} disabled={pending || action === "rename" && !name.trim()}>{pending ? "处理中…" : action === "delete" ? "确认删除" : "保存名称"}</button></div>
    </form>
  </Modal>;
}
