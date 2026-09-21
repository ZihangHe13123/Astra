import React, { useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import { ChevronRight, Command } from "lucide-react";
import type { CommandDescription } from "../bridge.js";
import { commandUsage, suggestCommands, type CommandMode, type CommandSuggestion } from "./command-help.js";
import { moveSelection } from "./control-state.js";

export function CommandInput({ inputRef, value, onChange, onSubmit, onPaste, disabled, placeholder, commands, modes, mode, attachments }: {
  inputRef: React.RefObject<HTMLTextAreaElement>; value: string; onChange: (value: string) => void; onSubmit: () => void;
  onPaste: React.ClipboardEventHandler<HTMLTextAreaElement>; disabled: boolean; placeholder: string;
  commands: CommandDescription[]; modes: CommandMode[]; mode: string; attachments: boolean;
}) {
  const [focused, setFocused] = useState(false);
  const [dismissed, setDismissed] = useState<string>();
  const [active, setActive] = useState(0);
  const listId = useId();
  const menu = useRef<HTMLDivElement>(null);
  const items = useMemo(() => suggestCommands(commands, value, modes, mode), [commands, value, modes, mode]);
  const visible = focused && !disabled && !attachments && value.startsWith("/") && !/[\r\n]/.test(value) && dismissed !== value;
  const activeIndex = Math.min(active, Math.max(0, items.length - 1));
  const selected = items[activeIndex];
  useLayoutEffect(() => { setActive(0); }, [value, mode]);
  useLayoutEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    const resize = () => { el.style.height = "auto"; el.style.height = `${Math.min(220, Math.max(36, el.scrollHeight))}px`; };
    resize();
    let width = el.clientWidth;
    const observer = new ResizeObserver(() => { if (width !== el.clientWidth) { width = el.clientWidth; resize(); } });
    observer.observe(el); return () => observer.disconnect();
  }, [value, disabled]);
  useLayoutEffect(() => { if (visible) menu.current?.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: "nearest" }); }, [activeIndex, visible, value]);
  const choose = (item: CommandSuggestion) => {
    const next = item.value + (item.more ? " " : "");
    onChange(next); setActive(0); setDismissed(item.more ? undefined : next);
    inputRef.current?.focus();
  };
  const hint = selected?.hint || commandUsage(value, modes);
  return <>
    {visible && <div className="command-suggestions" ref={menu}>
      <div className="command-suggestions-heading"><Command size={13}/><span>{modes.find(item => item.mode === mode)?.label || "全部"}指令</span><small>选中后填入，发送时执行</small></div>
      <div className="command-suggestions-list" id={listId} role="listbox" aria-label="指令建议">
        {items.map((item, index) => <button type="button" key={item.value} id={`${listId}-${index}`} role="option" aria-selected={index === activeIndex}
          className={index === activeIndex ? "selected" : ""} tabIndex={-1} onMouseDown={event => event.preventDefault()}
          onMouseMove={() => setActive(index)} onClick={() => choose(item)} title={item.hint || item.description}>
          <span><strong>{item.label}</strong><small>{item.description}</small></span><code>{item.value}</code>{item.more && <ChevronRight size={13}/>}
        </button>)}
        {!items.length && <p className="muted small" role="status">没有匹配的指令；可继续输入完整命令。</p>}
      </div>
      <div className="command-suggestions-foot">{hint && <span>{hint}</span>}<span>↑↓ 选择 · Tab / Enter 补全 · Esc 关闭</span></div>
    </div>}
    <textarea ref={inputRef} aria-label="消息" disabled={disabled} placeholder={placeholder} value={value} rows={1}
      aria-autocomplete="list" aria-haspopup="listbox" aria-controls={visible ? listId : undefined}
      aria-activedescendant={visible && selected ? `${listId}-${activeIndex}` : undefined}
      onFocus={() => setFocused(true)} onBlur={() => setFocused(false)}
      onChange={event => { setDismissed(undefined); onChange(event.target.value); }} onPaste={onPaste}
      onKeyDown={event => {
        if (event.nativeEvent.isComposing || event.keyCode === 229) return;
        if (visible && !event.shiftKey && !event.ctrlKey && !event.metaKey && !event.altKey) {
          if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); setDismissed(value); return; }
          if (items.length && (event.key === "ArrowDown" || event.key === "ArrowUp")) {
            event.preventDefault(); setActive(moveSelection(activeIndex, items.length, event.key === "ArrowDown" ? 1 : -1)); return;
          }
          if (selected && (event.key === "Tab" || event.key === "Enter" && (selected.more || selected.value !== value.trim()))) {
            event.preventDefault(); choose(selected); return;
          }
        }
        if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); setDismissed(value); onSubmit(); }
      }}/>
  </>;
}
