import { approvalChoiceLabel } from "@astra/ui-core/approval-choices";

/** Keyboard answers to a pending tool approval. An approval can run a tool, so nothing typed as text may count as one. */
export type ApprovalDecision = "once" | "task" | "session" | "deny";
export type ApprovalKeyPress = {
  key: string; metaKey?: boolean; ctrlKey?: boolean; shiftKey?: boolean; altKey?: boolean; repeat?: boolean; isComposing?: boolean;
};
/** The focused card root or an offered decision button, or outside all cards. Other descendants own their keys. */
export type ApprovalKeyPlace = { in: "card"; onButton: boolean } | { in: "window"; typing: boolean };

/**
 * The decision a key press gives, or undefined when it gives none.
 *
 * With Cmd or Ctrl held, Enter allows once, Shift+Enter allows for the session and Backspace denies.
 * Outside cards, a field holding text keeps sending and deleting. On an action button, a conflicting
 * Enter chord must not override the button or fall through to its native click.
 * Plain Y, T, A, N, Enter and Escape decide only on the focused card root. Enter on a button activates the button instead.
 * "以后都允许" has no key: a standing rule is added only by clicking it.
 * A held key repeats and never decides, so one press cannot answer the next request as well.
 */
export function approvalDecision(press: ApprovalKeyPress, place: ApprovalKeyPlace, choices: readonly string[]): ApprovalDecision | undefined {
  if (press.repeat || press.isComposing || press.altKey) return undefined;
  let decision: ApprovalDecision | undefined;
  if (press.metaKey || press.ctrlKey) {
    if (place.in === "window" && place.typing) return undefined;
    if (press.key === "Enter") decision = press.shiftKey ? "session" : "once";
    else if (press.key === "Backspace" && !press.shiftKey) decision = "deny";
  } else if (place.in === "card" && !place.onButton) {
    if (press.key === "Escape") decision = "deny";
    else if (press.key === "Enter") decision = press.shiftKey ? undefined : "once";
    else decision = ({ y: "once", t: "task", a: "session", n: "deny" } as Record<string, ApprovalDecision>)[press.key.toLowerCase()];
  }
  return decision && choices.includes(decision) ? decision : undefined;
}

/** The line under the buttons. Only the oldest pending approval answers the window-wide keys. */
export function approvalKeyHint(choices: readonly string[], windowKeys: boolean, mac: boolean, card: Record<string, unknown> = {}): string {
  const command = mac ? "⌘" : "Ctrl";
  const combined = [["once", `${command} Enter`], ["session", `${command} Shift Enter`], ["deny", `${command} ${mac ? "⌫" : "Backspace"}`]]
    .filter(([decision]) => choices.includes(decision)).map(([decision, keys]) => `${keys} ${approvalChoiceLabel(decision, card)}`);
  const letters = [["once", "Y"], ["task", "T"], ["session", "A"], ["deny", "N"]].filter(([decision]) => choices.includes(decision)).map(([, letter]) => letter);
  const release = card.kind === "computer_foreground_takeover" && choices.some(choice => choice !== "deny")
    ? ["键盘允许须松开按键；若未响应，请点击按钮"] : [];
  return [...(windowKeys ? combined : []), ...(letters.length ? [`点选卡片后可按 ${letters.join(" / ")}`] : []), ...release].join(" · ");
}

/** Whether the focus is in a field whose text the user would lose or send with the same keys. */
export function typingIn(field: { value?: unknown; textContent?: string | null } | null | undefined): boolean {
  if (!field) return false;
  return !!(typeof field.value === "string" ? field.value : field.textContent || "").trim();
}
