import React from "react";
import { CORE_TITLES, coreRoom, dateLabel, filterMemories, kindLabel, proposalLabel, statusLabel, usedLabel,
  type CoreEntry, type CoreFile, type MemoryOverview, type SavedMemory } from "./memory-view.js";

type Change = Parameters<typeof window.astra.memory>[0];

/** One core entry: read, edit in place, or remove. */
function CoreRow({ entry, busy, change }: { entry: CoreEntry; busy: boolean; change: (action: Change, ...values: string[]) => Promise<boolean> }) {
  const [draft, setDraft] = React.useState<string>();
  if (draft !== undefined) return <li className="memory-row editing">
    <textarea aria-label="编辑这条记忆" value={draft} onChange={event => setDraft(event.target.value)} rows={3}/>
    <div className="memory-actions">
      <button disabled={busy || !draft.trim() || draft === entry.content}
        onClick={() => { void change("core-edit", entry.id, entry.content, draft.trim()).then(done => { if (done) setDraft(undefined); }); }}>保存</button>
      <button disabled={busy} onClick={() => setDraft(undefined)}>取消</button>
    </div>
  </li>;
  return <li className="memory-row">
    <p>{entry.content}</p>
    <div className="memory-actions">
      <button disabled={busy} onClick={() => setDraft(entry.content)}>编辑</button>
      <button disabled={busy} onClick={() => { if (window.confirm("删除这条？")) void change("core-remove", entry.id); }}>删除</button>
    </div>
  </li>;
}

function CoreSection({ file, busy, change }: { file: CoreFile; busy: boolean; change: (action: Change, ...values: string[]) => Promise<boolean> }) {
  const [adding, setAdding] = React.useState("");
  const room = coreRoom(file);
  const title = CORE_TITLES[file.scope];
  return <section className="memory-core" data-scope={file.scope}>
    <h3>{title.title}</h3>
    <p className="muted small">{title.hint}</p>
    <div className={`memory-room ${room.nearlyFull ? "full" : ""}`} role="meter" aria-label={`${file.file} 已用`}
      aria-valuenow={room.percent} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${room.percent}%` }}/></div>
    <p className="muted small">{room.label}{room.nearlyFull ? "：快满了。可以删掉过时的条目，或者把细节交给「已保存的记忆」，这里只留一句话。" : ""}</p>
    <ul className="memory-list">{file.entries.map(entry => <CoreRow key={entry.id} entry={entry} busy={busy} change={change}/>)}</ul>
    <div className="memory-add">
      <input aria-label={`在${title.title}里加一条`} placeholder="加一条…" value={adding} onChange={event => setAdding(event.target.value)}/>
      <button disabled={busy || !adding.trim()} onClick={() => { void change("core-add", file.scope, adding.trim()).then(done => { if (done) setAdding(""); }); }}>添加</button>
    </div>
  </section>;
}

function SavedRow({ memory, busy, change }: { memory: SavedMemory; busy: boolean; change: (action: Change, ...values: string[]) => Promise<boolean> }) {
  const [open, setOpen] = React.useState(false);
  const [draft, setDraft] = React.useState<string>();
  const status = statusLabel(memory.status);
  return <li className={`memory-row saved ${status ? "inactive" : ""}`} data-memory={memory.id}>
    <button className="memory-head" aria-expanded={open} onClick={() => setOpen(!open)}>
      <span>{memory.summary}</span>
      <span className="muted small">{kindLabel(memory.kind)}{memory.scope === "project" ? " · 项目" : ""} · {dateLabel(memory.created_at)}{status ? ` · ${status}` : ""}</span>
    </button>
    {open && <>
      <p className="memory-content small">{memory.content}</p>
      <p className="muted small">{usedLabel(memory)}{memory.saved_by_astra ? " · Astra 在对话里记下的" : ""}{memory.source_session ? ` · 来自对话 ${memory.source_session}` : ""}</p>
      {!status && (draft === undefined
        ? <div className="memory-actions">
            <button disabled={busy} onClick={() => setDraft(memory.content)}>改正</button>
            <button disabled={busy} onClick={() => { if (window.confirm("忘记这条？她以后不会再用它；旧版本留在本机，可以在「显示旧版本」里看到。")) void change("forget", memory.id); }}>忘记</button>
          </div>
        : <>
            <textarea aria-label="改正这条记忆" value={draft} rows={4} onChange={event => setDraft(event.target.value)}/>
            <div className="memory-actions">
              <button disabled={busy || !draft.trim() || draft === memory.content}
                onClick={() => { void change("correct", memory.id, draft.trim()).then(done => { if (done) setDraft(undefined); }); }}>保存改正</button>
              <button disabled={busy} onClick={() => setDraft(undefined)}>取消</button>
            </div>
          </>)}
    </>}
  </li>;
}

/**
 * What Astra keeps about the person: the two core files (always in every conversation), saved memories
 * (an index is always visible, the text is read when needed) and consolidation proposals. Changes here are
 * the person's own and need no approval.
 */
export function MemoryPanel({ fail }: { fail: (error: unknown) => void }) {
  const [view, setView] = React.useState<MemoryOverview>();
  const [history, setHistory] = React.useState(false);
  const [filter, setFilter] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [notice, setNotice] = React.useState("");
  const load = React.useCallback(async () => setView(await window.astra.query("memory", { history }) as MemoryOverview), [history]);
  React.useEffect(() => { void load().catch(fail); }, [load, fail]);
  const change = async (action: Change, ...values: string[]) => {
    setBusy(true); setNotice("");
    try { await window.astra.memory(action, ...values); await load(); setNotice("已保存。下一轮对话起生效。"); return true; }
    catch (error) { fail(error); return false; }
    finally { setBusy(false); }
  };
  if (!view) return <p className="muted small memory-page">正在读取记忆…</p>;
  const saved = filterMemories(view.saved, filter);
  return <div className="memory-page">
    <p className="muted small">这里是 Astra 记着的关于你的东西。改动是你自己的，不需要批准，从下一轮对话起生效。删除一段对话不会删掉从中记下的记忆，要在这里忘记它。</p>
    {notice && <p className="small" role="status">{notice}</p>}
    {view.core.map(file => <CoreSection key={file.scope} file={file} busy={busy} change={change}/>)}
    <section className="memory-saved">
      <h3>已保存的记忆</h3>
      <p className="muted small">每条有一句摘要总在她眼前，需要时再读全文。</p>
      <div className="memory-filter">
        <input aria-label="搜索记忆" placeholder="搜索…" value={filter} onChange={event => setFilter(event.target.value)}/>
        <label className="activity-toggle small"><input type="checkbox" checked={history} onChange={event => setHistory(event.target.checked)}/> 显示旧版本和已忘记的</label>
      </div>
      {!saved.length && <p className="muted small">{filter ? "没有匹配的记忆。" : "还没有保存的记忆。"}</p>}
      <ul className="memory-list">{saved.map(memory => <SavedRow key={memory.id} memory={memory} busy={busy} change={change}/>)}</ul>
    </section>
    {!!view.proposals.length && <section className="memory-proposals">
      <h3>待确认的整理建议</h3>
      <ul className="memory-list">{view.proposals.map(proposal => <li key={proposal.id} className="memory-row" data-proposal={proposal.id}>
        <p><strong>{proposalLabel(proposal)}</strong>：{proposal.summary || proposal.content || proposal.previous}</p>
        {proposal.evidence.map((ref, index) => <p key={index} className="muted small">{ref.date} 你说过：{ref.text}</p>)}
        <div className="memory-actions">
          <button disabled={busy} onClick={() => { void change("accept", String(proposal.id)); }}>采纳</button>
          <button disabled={busy} onClick={() => { void change("dismiss", String(proposal.id)); }}>不用</button>
        </div>
      </li>)}</ul>
    </section>}
  </div>;
}
