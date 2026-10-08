/** Keyboard answers to a pending tool approval. An approval can run a tool, so nothing typed as text may count as one. */
export type ApprovalDecision = "once" | "session" | "deny";
export type ApprovalKeyPress = {
  key: string; metaKey?: boolean; ctrlKey?: boolean; shiftKey?: boolean; altKey?: boolean; repeat?: boolean; isComposing?: boolean;
};
/** Inside the approval card (possibly on one of its buttons), or anywhere else in the window (possibly in a field that holds text). */
export type ApprovalKeyPlace = { in: "card"; onButton: boolean } | { in: "window"; typing: boolean };

/**
 * The decision a key press gives, or undefined when it gives none.
 *
 * With Cmd or Ctrl held, Enter allows once, Shift+Enter allows for the session and Backspace denies, from
 * anywhere in the window except a field that holds text, where those keys keep sending and deleting.
 * Plain Y, A, N, Enter and Escape decide only inside the card, which never takes the focus by itself.
 * A held key repeats and never decides, so one press cannot answer the next request as well.
 */
export function approvalDecision(press: ApprovalKeyPress, place: ApprovalKeyPlace, choices: readonly string[]): ApprovalDecision | undefined {
  if (press.repeat || press.isComposing || press.altKey) return undefined;
  let decision: ApprovalDecision | undefined;
  if (press.metaKey || press.ctrlKey) {
    if (place.in === "window" && place.typing) return undefined;
    if (press.key === "Enter") decision = press.shiftKey ? "session" : "once";
    else if (press.key === "Backspace" && !press.shiftKey) decision = "deny";
  } else if (place.in === "card") {
    if (press.key === "Escape") decision = "deny";
    // Enter on a focused button presses that button, whichever it is.
    else if (press.key === "Enter") decision = place.onButton || press.shiftKey ? undefined : "once";
    else decision = ({ y: "once", a: "session", n: "deny" } as Record<string, ApprovalDecision>)[press.key.toLowerCase()];
  }
  return decision && choices.includes(decision) ? decision : undefined;
}

/** The line under the buttons. Only the oldest pending approval answers the window-wide keys. */
export function approvalKeyHint(choices: readonly string[], windowKeys: boolean, mac: boolean): string {
  const command = mac ? "⌘" : "Ctrl";
  const combined = [["once", `${command} Enter 允许一次`], ["session", `${command} Shift Enter 本会话允许`], ["deny", `${command} ${mac ? "⌫" : "Backspace"} 拒绝`]]
    .filter(([decision]) => choices.includes(decision)).map(([, text]) => text);
  const letters = [["once", "Y"], ["session", "A"], ["deny", "N"]].filter(([decision]) => choices.includes(decision)).map(([, letter]) => letter);
  return [...(windowKeys ? combined : []), ...(letters.length ? [`点选卡片后可按 ${letters.join(" / ")}`] : [])].join(" · ");
}

/** Whether the focus is in a field whose text the user would lose or send with the same keys. */
export function typingIn(field: { value?: unknown; textContent?: string | null } | null | undefined): boolean {
  if (!field) return false;
  return !!(typeof field.value === "string" ? field.value : field.textContent || "").trim();
}
