import React from "react";
import { ChevronRight } from "lucide-react";
import type { UIEvent } from "@astra/ui-core/session-state";
import type { CommandDescription } from "../bridge.js";

export type ReviewMode = "off" | "review" | "auto";

export const REVIEW_CHOICES: { mode: ReviewMode; name: string; description: string }[] = [
  { mode: "off", name: "关闭", description: "不在后台整理记忆" },
  { mode: "review", name: "需审核", description: "提出修改建议，你逐条确认" },
  { mode: "auto", name: "自动同意", description: "记住和修订直接生效，遗忘仍等你确认" },
];

/** The background memory review as the desktop shows it. The backend's latest memory_consolidation_status is the only source. */
export function reviewView(status: UIEvent | undefined): { mode?: ReviewMode; pending: number } {
  const mode = REVIEW_CHOICES.find(choice => choice.mode === status?.mode)?.mode;
  return { mode, pending: typeof status?.pending === "number" && status.pending > 0 ? status.pending : 0 };
}

/** The answer to a /memory consolidation command the user typed, told from the status the backend reports back. */
export function reviewAnswer(action: string, status: UIEvent): string {
  if (status.error) return `记忆整理：${status.error}`;
  if (action === "now") return "将在下一个空闲时刻整理一次记忆";
  const view = reviewView(status);
  const name = REVIEW_CHOICES.find(choice => choice.mode === view.mode)?.name ?? "未知模式";
  return `记忆整理：${name}${view.pending ? `，${view.pending} 条建议待审核` : ""}`;
}

/** The mode belongs to the installation; the session's backend saves each choice and reports it back. */
export function MemoryReviewSettings({ status, ready, run, review }: {
  status: UIEvent | undefined; ready: boolean; run: (command: string) => void; review: () => void;
}) {
  const view = reviewView(status);
  return <>
    <h3>记忆整理</h3>
    <p className="muted small">Astra 空闲时回顾新的对话，整理你说过的长期偏好和事实。设置对这台电脑上的所有会话生效。</p>
    <div className="choice-grid three">{REVIEW_CHOICES.map(choice =>
      <button key={choice.mode} disabled={!ready} aria-pressed={view.mode === choice.mode} onClick={() => run(`/memory consolidation ${choice.mode}`)}><strong>{choice.name}</strong><small>{choice.description}</small></button>)}</div>
    {!ready && <p className="muted small">打开一个会话后可以切换。</p>}
    {ready && view.pending > 0 && <button className="text-button" onClick={review}>{view.pending} 条建议待审核 <ChevronRight size={14}/></button>}
  </>;
}

const ACTIONS: Record<string, string> = { remember: "记住", revise: "修订", forget: "忘记" };

/** Offer each waiting proposal under /memory accept and dismiss, next to the registered actions. */
export function withProposalOptions(command: CommandDescription, status: UIEvent | undefined): CommandDescription {
  const proposals = Array.isArray(status?.proposals) ? status.proposals.filter((item: unknown): item is { id: number; action: string; summary: string } =>
    !!item && typeof (item as { id?: unknown }).id === "number" && typeof (item as { summary?: unknown }).summary === "string") : [];
  if (!proposals.length) return command;
  const option = (label: string, description: string, value: string) => ({ command: label, description, completion: value, submitValue: value });
  return { ...command, options: [...command.options,
    ...proposals.flatMap(item => [
      option(`接受 P${item.id}`, `${ACTIONS[item.action] ?? item.action}：${item.summary}`, `/memory accept ${item.id}`),
      option(`丢弃 P${item.id}`, `不${ACTIONS[item.action] ?? item.action}：${item.summary}`, `/memory dismiss ${item.id}`)]),
    option("全部接受", `应用全部 ${proposals.length} 条建议`, "/memory accept all"),
    option("全部丢弃", `丢弃全部 ${proposals.length} 条建议`, "/memory dismiss all")] };
}
