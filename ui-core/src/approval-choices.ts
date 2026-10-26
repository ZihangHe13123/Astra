/**
 * What an approval card offers and how each answer reads, the same on the desktop, the phone and the terminal.
 *
 * - once: this call only. task: until the work of the person's current message is done.
 * - session: until this conversation ends (each WeChat chat and the desktop session apart).
 * - always: a standing rule, offered only after the same request was approved often and never refused.
 * A consequential request (pay, send, delete, publish) offers once and deny only; with a draft, they read
 * as 发送 and 丢弃.
 */
export type ApprovalChoice = "once" | "task" | "session" | "always" | "deny";
export const APPROVAL_CHOICES: readonly ApprovalChoice[] = ["once", "task", "session", "always", "deny"];
const LABELS: Record<ApprovalChoice, string> = {
  once: "允许一次", task: "本任务允许", session: "本会话允许", always: "以后都允许", deny: "拒绝",
};
export const CONSEQUENCE_LABELS: Record<string, string> = { pay: "付款", send: "发送", delete: "删除", publish: "发布" };

export type ApprovalDraft = { to?: string; subject?: string; text?: string; via?: string };
type Card = { choices?: readonly string[]; consequential?: string; draft?: ApprovalDraft | null; [key: string]: unknown };

/** The answers a card shows, in order; an old request without choices keeps the three it always had. */
export function approvalChoices(card: Card): ApprovalChoice[] {
  const offered = (card.choices?.length ? card.choices : ["once", "session", "deny"])
    .filter((choice): choice is ApprovalChoice => (APPROVAL_CHOICES as readonly string[]).includes(choice));
  return card.consequential ? offered.filter(choice => choice === "once" || choice === "deny") : offered;
}

export function approvalChoiceLabel(choice: string, card: Card): string {
  if (card.draft?.text) {
    if (choice === "once") return "发送";
    if (choice === "deny") return "丢弃";
  }
  return LABELS[choice as ApprovalChoice] || choice;
}

/** The line that says a request is asked every time, or "" for an ordinary one. */
export function consequenceNote(card: Card): string {
  if (!card.consequential) return "";
  const what = CONSEQUENCE_LABELS[card.consequential] || "高影响";
  return `${what}类操作：每次都会单独问你，之前给过的授权不覆盖它。`;
}
