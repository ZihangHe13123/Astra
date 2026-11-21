import assert from "node:assert/strict";
import test from "node:test";
import { bindApprovalKeyboard } from "../src/renderer/approval-keyboard.js";

// Dispatch capture, card and window listeners in DOM order without opening a window. A native
// button click is a default action: Enter clicks on keydown, Space on keyup unless suppressed.
class Listeners {
  entries: { type: string; fn: Function; capture: boolean; signal?: AbortSignal }[] = [];
  addEventListener(type: string, fn: Function, options: boolean | AddEventListenerOptions = {}) {
    this.entries.push({ type, fn, ...(typeof options === "boolean" ? { capture: options } : { ...options, capture: !!options.capture }) });
  }
  removeEventListener(type: string, fn: Function) { this.entries = this.entries.filter(x => x.type !== type || x.fn !== fn); }
  emit(type: string, e: object, capture: boolean) {
    for (const item of [...this.entries]) if (item.type === type && item.capture === capture && !item.signal?.aborted) item.fn(e);
  }
}

function fixture(foreground = true) {
  const sent: string[] = [], win = new Listeners();
  const doc = Object.assign(new Listeners(), { defaultView: win, activeElement: null as Element | null, hidden: false,
    hasFocus: () => true, documentElement: { getBoundingClientRect: () => ({ top: 0, bottom: 100 }) } });
  const state = { requestId: "first", foreground, windowKeys: true, pending: false, choices: ["once", "session", "deny"],
    decide: (decision: string) => { sent.push(`${state.requestId}:${decision}`); } };
  let offscreen = false, reveals = 0;
  const row = { getBoundingClientRect: () => ({ top: 10, bottom: offscreen ? 101 : 90 }) };
  const card = Object.assign(new Listeners(), { ownerDocument: doc, isConnected: true, querySelector: () => row,
    closest: (selector: string): unknown => selector === ".approval" ? card : null, scrollIntoView: () => { reveals++; } });
  const element = (where: "field" | "button", choice = "") => {
    const el = { isConnected: true, value: "", dataset: { approvalChoice: choice },
      closest: (selector: string): unknown => selector === ".approval" ? (where === "button" ? card : null)
        : selector.startsWith("button") ? (where === "button" ? el : null) : where === "field" ? el : null };
    return el;
  };
  const field = element("field"), once = element("button", "once"), session = element("button", "session"), deny = element("button", "deny");
  const detach = bindApprovalKeyboard(card as unknown as HTMLElement, () => state);
  const input = (type: string, key: string, flags: Partial<KeyboardEvent> = {}, target: typeof field | typeof card = field) => {
    const e = { target, key, code: key.length === 1 && key !== " " ? `Key${key.toUpperCase()}` : key,
      defaultPrevented: false, cancelBubble: false, preventDefault() { this.defaultPrevented = true; },
      stopPropagation() { this.cancelBubble = true; }, ...flags };
    win.emit(type, e, true);
    if (target.closest(".approval") === card && !e.cancelBubble) card.emit(type, e, false);
    if (!e.cancelBubble) win.emit(type, e, false);
    return e;
  };
  const focus = (target: unknown) => { doc.activeElement = target as Element; };
  focus(field);
  return { state, sent, field, card, once, session, deny, focus, input,
    down: (key: string, flags?: Partial<KeyboardEvent>, target?: typeof field | typeof card) => input("keydown", key, flags, target),
    up: (key: string, flags?: Partial<KeyboardEvent>, target?: typeof field | typeof card) => input("keyup", key, flags, target),
    unmount: detach,
    setOffscreen: (value: boolean) => { offscreen = value; }, reveals: () => reveals };
}

test("a foreground shortcut sends once, after the approval key AND its modifiers are up in either order", () => {
  for (const modsFirst of [true, false]) {
    const f = fixture();
    assert.ok(f.down("Enter", { metaKey: true, shiftKey: true }).defaultPrevented);
    assert.deepEqual(f.sent, []);
    if (modsFirst) {
      f.up("Meta", { shiftKey: true }); f.up("Shift");
      assert.deepEqual(f.sent, []); f.up("Enter");
    } else {
      f.up("Enter", { metaKey: true, shiftKey: true }); f.up("Shift", { metaKey: true });
      assert.deepEqual(f.sent, []); f.up("Meta");
    }
    f.up("Enter"); f.up("Meta");
    assert.deepEqual(f.sent, ["first:session"]);
  }
});

test("on a foreground button native Enter cannot click early; Space and card letter shortcuts also wait", () => {
  for (const key of ["Enter", " ", "A"]) {
    const f = fixture(); f.focus(f.session);
    const down = f.down(key, { shiftKey: true }, f.session);
    if (!down.defaultPrevented && key === "Enter") f.state.decide("session");
    assert.deepEqual(f.sent, []);
    const up = f.up(key, { shiftKey: true }, f.session);
    if (!down.defaultPrevented && !up.defaultPrevented && key === " ") f.state.decide("session");
    assert.deepEqual(f.sent, []); f.up("Shift", {}, f.session);
    assert.deepEqual(f.sent, ["first:session"]);
  }
});

test("repeat, a different key or physical keyup, blur/focus change, click, and stale requests cannot finish a gesture", () => {
  for (const interrupt of ["repeat", "keydown", "wrong code", "blur", "focusout", "pointerdown", "click", "wheel", "request", "choice", "global", "unmount"]) {
    const f = fixture(); f.down("Enter", { ctrlKey: true });
    if (interrupt === "repeat") f.down("Enter", { ctrlKey: true, repeat: true });
    else if (interrupt === "keydown") assert.equal(f.down("k", { ctrlKey: true }).defaultPrevented, false);
    else if (interrupt === "wrong code") f.up("Enter", { code: "NumpadEnter", ctrlKey: true });
    else if (interrupt === "request") f.state.requestId = "next";
    else if (interrupt === "choice") f.state.choices = ["deny"];
    else if (interrupt === "global") f.state.windowKeys = false;
    else if (interrupt === "unmount") f.unmount();
    else f.input(interrupt, "");
    f.up("Enter", { ctrlKey: true }); f.up("Control");
    assert.deepEqual(f.sent, [], interrupt);
  }
  const f = fixture(); f.focus(f.once); f.down("y", {}, f.once); f.focus(f.session); f.up("y", {}, f.session);
  assert.deepEqual(f.sent, []);
  f.focus(f.field); f.down("y", {}, f.card); f.up("y", {}, f.card); // not the focused card
  assert.deepEqual(f.sent, []);
});

test("repeating button Enter cannot click, and a real or accessible button click cancels without being swallowed", () => {
  const f = fixture(); f.focus(f.once); f.down("Enter", {}, f.once);
  assert.ok(f.down("Enter", { repeat: true }, f.once).defaultPrevented); f.up("Enter", {}, f.once);
  assert.deepEqual(f.sent, []);
  f.down("Enter", {}, f.once);
  const click = f.input("click", "", {}, f.once);
  assert.equal(click.defaultPrevented || click.cancelBubble, false);
  f.state.decide("once"); // untouched button onClick, including an accessibility click with no prior pointerdown
  f.up("Enter", {}, f.once);
  assert.deepEqual(f.sent, ["first:once"]);
});

test("missing Chromium command-chord keyup does not approve or prevent a fresh independent answer", () => {
  const f = fixture(); f.down("Enter", { metaKey: true }); f.up("Meta");
  assert.deepEqual(f.sent, []);
  f.down("Control", { ctrlKey: true }); f.down("Enter", { ctrlKey: true }); f.up("Enter", { ctrlKey: true }); f.up("Control");
  assert.deepEqual(f.sent, ["first:once"]);
});

test("scroll first, typing exclusions, and immediate denial / non-foreground behavior are unchanged", () => {
  const f = fixture(); f.setOffscreen(true); f.down("Enter", { metaKey: true }); f.up("Enter"); f.up("Meta");
  assert.equal(f.reveals(), 1); assert.deepEqual(f.sent, []);
  f.setOffscreen(false); f.field.value = "draft";
  assert.equal(f.down("Enter", { metaKey: true }).defaultPrevented, false);
  f.field.value = ""; f.down("Backspace", { ctrlKey: true }); assert.deepEqual(f.sent, ["first:deny"]);
  const g = fixture(false); g.down("Enter", { ctrlKey: true }); assert.deepEqual(g.sent, ["first:once"]);
  g.focus(g.session); assert.equal(g.down("Enter", {}, g.session).defaultPrevented, false);
});
