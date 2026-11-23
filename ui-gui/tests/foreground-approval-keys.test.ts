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
  const details = { open: false };
  const card = Object.assign(new Listeners(), { ownerDocument: doc, isConnected: true, querySelector: () => row,
    closest: (selector: string): unknown => selector === ".approval" ? card : null, scrollIntoView: () => { reveals++; } });
  const element = (tag: string, parentElement: object | null, choice = "") => {
    const el = { tagName: tag.toUpperCase(), parentElement, disabled: false, isConnected: true, isContentEditable: false, tabIndex: -1,
      value: "", dataset: { approvalChoice: choice },
      closest: (selector: string): unknown => selector === ".approval" ? (el.parentElement ? card : null)
        : selector.startsWith("button") ? (el.tagName === "BUTTON" ? el : null) : el.isContentEditable || ["INPUT", "TEXTAREA"].includes(el.tagName) ? el : null };
    return el;
  };
  const field = element("input", null), once = element("button", row, "once"), session = element("button", row, "session"), deny = element("button", row, "deny");
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
  return { state, sent, field, card, once, session, deny, focus, input, details, row, element,
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

test("on a foreground decision button native Enter cannot click early and Space also waits", () => {
  for (const key of ["Enter", " "]) {
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
  const f = fixture(); f.focus(f.once); f.down("Enter", {}, f.once); f.focus(f.session); f.up("Enter", {}, f.session);
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

test("Tab to the details summary and Enter toggle disclosure without answering either kind of card", () => {
  for (const foreground of [true, false]) {
    const f = fixture(foreground), summary = f.element("summary", f.details);
    f.focus(f.card); assert.equal(f.down("Tab", {}, f.card).defaultPrevented, false);
    f.focus(summary); // native Tab traversal
    const down = f.down("Enter", {}, summary);
    if (!down.defaultPrevented) f.details.open = !f.details.open; // native summary activation
    assert.equal(f.details.open, true);
    for (const e of [down, f.down("Enter", { repeat: true }, summary), f.up("Enter", {}, summary)]) {
      assert.equal(e.defaultPrevented || e.cancelBubble, false);
    }
    assert.deepEqual(f.sent, []);
  }
});

test("a focused non-decision descendant owns its keys, even when it resembles an offered decision button", () => {
  for (const foreground of [true, false]) {
    const f = fixture(foreground);
    const editable = Object.assign(f.element("div", f.details), { isContentEditable: true });
    const tabStop = Object.assign(f.element("div", f.card), { tabIndex: 0 });
    const children = [editable, tabStop, f.element("button", f.row, "unoffered"), f.element("span", f.row, "once"),
      ...["summary", "pre", "a", "button", "input", "textarea", "select", "h3", "span"].map(tag => f.element(tag, f.details, "once"))];
    for (const child of children) {
      f.focus(child);
      for (const key of ["Enter", " ", "Y", "T", "A", "N", "Escape"]) {
        for (const flags of [{}, { ctrlKey: true }]) {
          const down = f.down(key, flags, child), up = f.up(key, flags, child);
          assert.equal(down.defaultPrevented || down.cancelBubble || up.defaultPrevented, false, `${child.tagName}: ${key}`);
          if ("ctrlKey" in flags) f.up("Control", {}, child);
        }
      }
      assert.deepEqual(f.sent, [], child.tagName);
    }
  }
});

test("only a focused card root owns its plain letter shortcuts, never the focused deny or another choice button", () => {
  for (const foreground of [true, false]) {
    const f = fixture(foreground); f.state.choices.push("task");
    for (const button of [f.once, f.session, f.deny]) {
      f.focus(button);
      for (const key of ["Y", "T", "A", "N", "Escape"]) {
        assert.equal(f.down(key, {}, button).defaultPrevented, false); f.up(key, {}, button);
      }
      assert.deepEqual(f.sent, []);
    }
    f.focus(f.card); f.down("A", {}, f.card); f.up("A", {}, f.card);
    assert.deepEqual(f.sent, ["first:session"]);
    f.focus(f.deny); const enter = f.down("Enter", {}, f.deny);
    if (!enter.defaultPrevented) f.state.decide("deny"); // native deny button click
    f.up("Enter", {}, f.deny); assert.deepEqual(f.sent, ["first:session", "first:deny"]);
  }
});

test("a retargeted, disabled or repurposed button cannot complete an earlier foreground choice", () => {
  for (const change of ["row", "choice", "disabled"]) {
    const f = fixture(); f.focus(f.once); f.down("Enter", {}, f.once);
    if (change === "row") f.once.parentElement = f.details;
    if (change === "choice") f.once.dataset.approvalChoice = "deny";
    if (change === "disabled") f.once.disabled = true;
    f.up("Enter", {}, f.once); assert.deepEqual(f.sent, [], change);
  }
  for (const foreground of [true, false]) {
    const f = fixture(foreground); f.focus(f.element("summary", f.details));
    f.down("Enter", {}, f.card); f.up("Enter", {}, f.card); assert.deepEqual(f.sent, []);
  }
});

test("explicit chords can choose the focused button but a mismatched Enter chord cannot switch its choice", () => {
  for (const foreground of [true, false]) {
    const f = fixture(foreground);
    for (const [button, shiftKey] of [[f.once, false], [f.session, true], [f.deny, false], [f.once, true]] as const) {
      f.focus(button);
      const flags = { ctrlKey: true, shiftKey };
      const down = f.down("Enter", flags, button);
      if (!down.defaultPrevented) f.state.decide(button.dataset.approvalChoice); // no stray native activation, either
      f.up("Enter", flags, button);
      if (shiftKey) f.up("Shift", { ctrlKey: true }, button);
      f.up("Control", {}, button);
    }
    assert.deepEqual(f.sent, ["first:once", "first:session"]);
  }
});
