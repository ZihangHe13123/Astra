import { approvalDecision, typingIn } from "./approval-keys.js";

type KeyboardCard = {
  requestId: string; foreground: boolean; windowKeys: boolean; pending: boolean; choices: readonly string[]; decide(decision: string): void;
};
const modifiers = [["Meta", "metaKey"], ["Control", "ctrlKey"], ["Shift", "shiftKey"], ["Alt", "altKey"]] as const;
const fieldSelector = "input,textarea,[contenteditable='true']";

/** A key inside a card belongs to that card; only the oldest eligible card listens outside. */
export function bindApprovalKeyboard(card: HTMLElement, read: () => KeyboardCard): () => void {
  const doc = card.ownerDocument, win = doc.defaultView!;
  let release: AbortController | undefined;
  const cancel = () => { release?.abort(); release = undefined; };
  const interrupted = new WeakSet<KeyboardEvent>();
  function revealIfHidden() {
    const row = card.querySelector(".actions")?.getBoundingClientRect();
    const view = (card.closest(".messages-scroll") || doc.documentElement).getBoundingClientRect();
    if (!row || row.top >= view.top && row.bottom <= view.bottom) return false;
    card.scrollIntoView({ block: "nearest" }); return true;
  }

  // A foreground answer starts a physical-input check at the backend. Its own key releases must
  // precede the answer, not look like the person took over afterwards. Never infer Enter-up from
  // Meta-up: Chromium can omit that keyup, but Enter could also actually still be down.
  function waitForRelease(down: KeyboardEvent, target: Element, outside: boolean, decision: string) {
    cancel();
    const requestId = read().requestId, focus = doc.activeElement;
    if (target !== focus || !target.isConnected || !doc.hasFocus() || doc.hidden) return;
    const held = new Set<string>(modifiers.filter(([, flag]) => down[flag]).map(([key]) => key));
    let keyUp = false;
    release = new AbortController();
    const options = { capture: true, signal: release.signal };
    win.addEventListener("keydown", e => { interrupted.add(e); cancel(); }, options);
    for (const type of ["blur", "focusout", "focusin", "pagehide", "pointerdown", "pointerup", "pointermove", "pointercancel", "click", "wheel", "beforeinput", "input", "compositionstart"]) {
      win.addEventListener(type, cancel, options);
    }
    doc.addEventListener("visibilitychange", cancel, options);
    win.addEventListener("keyup", e => {
      const state = read();
      const owner = target.closest(".approval");
      const samePlace = outside ? state.windowKeys && !owner && !typingIn(target.closest<HTMLInputElement>(fieldSelector)) : owner === card;
      if (e.isComposing || e.repeat || e.target !== target || doc.activeElement !== focus || !doc.hasFocus() || doc.hidden
          || !card.isConnected || !target.isConnected || state.requestId !== requestId || !state.foreground || state.pending
          || !state.choices.includes(decision) || !samePlace) { cancel(); return; }
      if (down.code ? e.code === down.code : e.key.toLowerCase() === down.key.toLowerCase()) {
        if (keyUp) { cancel(); return; }
        keyUp = true; e.preventDefault();
      } else {
        const modifier = modifiers.find(([key]) => key === e.key);
        if (!modifier || !held.has(modifier[0])) { cancel(); return; }
        if (!e[modifier[1]]) held.delete(modifier[0]); // either side can still be held
      }
      if (!keyUp || held.size) return;
      cancel();
      if (!modifiers.some(([, flag]) => e[flag]) && (!outside || !revealIfHidden())) state.decide(decision);
    }, options);
  }

  function keydown(e: KeyboardEvent, outside: boolean) {
    const state = read(), target = e.target as Element | null;
    const owner = target?.closest?.(".approval");
    if (outside ? owner || !state.windowKeys : owner !== card) return;
    const button = target?.closest?.<HTMLButtonElement>("button");
    const place = outside
      ? { in: "window", typing: typingIn(target?.closest?.<HTMLInputElement>(fieldSelector)) } as const
      : { in: "card", onButton: !!button } as const;
    let decision: string | undefined = approvalDecision(e, place, state.choices);
    const nativeChoice = !outside && state.foreground && (e.key === "Enter" || e.key === " ") ? button?.dataset.approvalChoice : undefined;
    // Enter activates a focused button on keydown in Chromium. Suppress native keyboard activation
    // even for a cancelled/repeating press, but leave mouse and accessibility clicks alone.
    const positiveButton = nativeChoice && nativeChoice !== "deny" && state.choices.includes(nativeChoice);
    if (!decision) {
      if (!positiveButton) return;
      e.preventDefault(); // no native click; unrelated modified shortcuts still bubble
      if (e.repeat || e.isComposing || e.altKey || e.ctrlKey || e.metaKey) return;
      decision = nativeChoice;
    }
    e.preventDefault(); if (!outside) e.stopPropagation();
    if (!decision || state.pending || state.foreground && decision !== "deny" && interrupted.has(e)) return;
    if (outside && revealIfHidden()) return;
    if (!state.foreground || decision === "deny") state.decide(decision);
    else if (target) waitForRelease(e, target, outside, decision);
  }
  const local = (e: KeyboardEvent) => keydown(e, false), global = (e: KeyboardEvent) => keydown(e, true);
  card.addEventListener("keydown", local);
  if (read().windowKeys) win.addEventListener("keydown", global);
  return () => { cancel(); card.removeEventListener("keydown", local); win.removeEventListener("keydown", global); };
}
