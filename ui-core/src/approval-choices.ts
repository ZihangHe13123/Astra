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

export const draftLabels = { to: "发给", cc: "抄送", bcc: "密送", destination: "目标位置", subject: "主题", via: "通过" } as const;
export type ApprovalDraft = Partial<Record<keyof typeof draftLabels | "text", string>> & { truncated?: boolean };
const draftFields = new Set(["text", ...Object.keys(draftLabels)]);

function validDestination(raw: string): boolean {
  // The source encodes a flat JSON object of named bindings. Validate its shape, but render the
  // original string: reserializing would silently round integer identifiers beyond JS precision.
  try {
    const bindings: unknown = JSON.parse(raw);
    if (!bindings || typeof bindings !== "object" || Array.isArray(bindings)) return false;
    const entries = Object.entries(bindings);
    return entries.length > 0 && entries.every(([key, id]) => key.trim() !== "" &&
      (typeof id === "string" && id.trim() !== "" || Number.isInteger(id)));
  } catch { return false; }
}

/** An unknown/redacted field or a bounded preview is never a complete send envelope. */
export function completeApprovalDraft(value: unknown, maxUnits = Infinity): value is ApprovalDraft & { text: string } {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const draft = value as Record<string, unknown>;
  if (typeof draft.text !== "string" || !draft.text.length || ("truncated" in draft && draft.truncated !== false)) return false;
  let size = 0;
  return Object.entries(draft).every(([key, v]) => key === "truncated" || draftFields.has(key) &&
    typeof v === "string" && (size += v.length) <= maxUnits && !/[\ud800-\udfff]/u.test(v) &&
    (key !== "destination" || validDestination(v)));
}
type Card = { choices?: readonly string[]; consequential?: string; draft?: ApprovalDraft | null; [key: string]: unknown };

/** The answers a card shows, in order; an old request without choices keeps the three it always had. */
export function approvalChoices(card: Card): ApprovalChoice[] {
  if ("draft" in card) {
    const once = card.choices === undefined || Array.isArray(card.choices) && card.choices.includes("once");
    return once && card.risk !== "secret" && completeApprovalDraft(card.draft) ? ["once", "deny"] : ["deny"];
  }
  const offered = (card.choices?.length ? card.choices : ["once", "session", "deny"])
    .filter((choice): choice is ApprovalChoice => (APPROVAL_CHOICES as readonly string[]).includes(choice));
  return card.consequential ? offered.filter(choice => choice === "once" || choice === "deny") : offered;
}

export function approvalChoiceLabel(choice: string, card: Card): string {
  if (choice === "session" && card.kind === "computer_foreground_takeover") return "借用此窗口，最多 2 分钟";
  if ("draft" in card) {
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
