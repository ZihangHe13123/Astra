import React from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import type { Message } from '@astra/ui-core/session-state';
import { sameResponseSource, type ResponseGroup, type ResponseRegeneration, type ResponseSource, type ResponseVersions } from '@astra/ui-core/response-versions';
export type ResponseControls = {
  versions: ResponseVersions; regeneration?: ResponseRegeneration; busy: boolean;
  regenerate: (source: ResponseSource) => void; select: (group: string, version: string) => void;
};
export function responseForMessage(message: Message, controls?: ResponseControls) {
  const group = message.role === 'assistant' ? controls?.versions.groups.find(g => sameResponseSource(g.source_ref, message.source_ref)) : undefined;
  const target = message.role === 'assistant' ? controls?.versions.targets.find(t => sameResponseSource(t.source_ref, message.source_ref)) : undefined;
  const pending = message.role === 'assistant' && sameResponseSource(controls?.regeneration?.source_ref, message.source_ref);
  return { group, target, pending, content: pending ? controls!.regeneration!.content : message.content };
}
export function ResponseVersionNav({ group, busy, select }: { group: ResponseGroup; busy: boolean; select: (group: string, version: string) => void }) {
  const versions = group.versions.filter(v => v.status === 'completed');
  if (versions.length < 2) return null;
  const current = versions.findIndex(v => v.id === group.selected_version);
  return <div className="response-versions" role="group" aria-label="回复版本" data-response-group={group.id}>
    <button className="icon" aria-label="上一个回复版本" disabled={busy || current <= 0} onClick={() => select(group.id, versions[current - 1].id)}><ChevronLeft size={14}/></button>
    <span aria-live="polite">{current >= 0 ? current + 1 : '—'} / {versions.length}</span>
    <button className="icon" aria-label="下一个回复版本" disabled={busy || current < 0 || current >= versions.length - 1} onClick={() => select(group.id, versions[current + 1].id)}><ChevronRight size={14}/></button>
  </div>;
}
export function EarlierResponseVersions({ messages, controls }: { messages: Message[]; controls?: ResponseControls }) {
  const groups = controls?.versions.groups.filter(group => group.versions.filter(v => v.status === 'completed').length > 1
    && !messages.some(m => m.role === 'assistant' && sameResponseSource(m.source_ref, group.source_ref))) || [];
  if (!controls || !groups.length) return null;
  return <details className="earlier-response-versions"><summary>较早回复版本</summary>
    <p className="small muted">选择已保存的回复版本，后续对话会沿所选版本继续。现实文件和工具操作不会撤销。</p>
    {groups.map(group => <div className="earlier-response-version" key={group.id} data-response-group-id={group.id}>
      <p>{group.user_text || '较早的请求'}</p><p className="small muted">{group.response_text}</p>
      <ResponseVersionNav group={group} busy={controls.busy} select={controls.select}/>
    </div>)}
  </details>;
}
