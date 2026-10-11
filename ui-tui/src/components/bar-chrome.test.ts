import assert from "node:assert/strict";
import stringWidth from "string-width";
import {
  barSign,
  formatBarClock,
  formatBarRoom,
  isQuietBarAction,
} from "./bar-chrome.js";
import type { BarAmbianceState } from "../types.js";

assert.match(formatBarClock(new Date(2026, 6, 17, 23, 7).getTime()), /^23:07$/);
assert.equal(isQuietBarAction("/sip", "bar"), true);
assert.equal(isQuietBarAction("/bar sip", "bar"), true);
assert.equal(isQuietBarAction("/sip", "work"), false);
assert.equal(isQuietBarAction("I sip the drink", "bar"), false);

const storm: BarAmbianceState = { weather: "downpour", power: "flicker", music: "old_radio", radio: "local_news" };
const calm: BarAmbianceState = { weather: "clearing", power: "stable", music: "silent", radio: "static" };

// The room is told in phrases when they fit, in bare words when they do not, and in fewer
// words before any word is cut.
assert.equal(formatBarRoom(storm), "downpour outside · lights flickering · ♪ old radio · local news on air");
assert.equal(formatBarRoom(calm), "sky clearing · lights steady · no music · radio static");
assert.equal(formatBarRoom(storm, 50), "downpour · flicker · old radio · local news");
assert.equal(formatBarRoom(storm, 30), "downpour · flicker · old radio");
assert.equal(formatBarRoom(storm, 12), "downpour");
for (const width of [70, 50, 30, 12, 6]) assert.ok(stringWidth(formatBarRoom(storm, width)) <= width, `room at ${width}`);
// A value this client has no phrase for is still shown.
assert.equal(
  formatBarRoom({ ...calm, weather: "acid_fog" } as unknown as BarAmbianceState),
  "acid fog · lights steady · no music · radio static",
);

const now = new Date(2026, 6, 17, 23, 7).getTime();
const base = {
  session: "rainy-thursday",
  busy: false,
  disconnected: false,
  ambiance: storm,
  shift: { turn_count: 6, phase: "deep" as const },
  lyraGlass: { active: true, name: "夜班清水", note: "柠檬", fill: 2 },
  outputMode: "atomic" as const,
  now,
};

/** The widest row the sign would draw, measured the way the frame draws it. */
function widest(sign: ReturnType<typeof barSign>): number {
  const aside = [sign.output, sign.leave].filter(Boolean).join(" · ");
  const beside = sign.session || (sign.compact ? sign.notice : "");
  const rows = [
    [sign.title, [sign.phase, sign.clock].filter(Boolean).join("  ")],
    [`${sign.mark} ${sign.state}${beside ? ` · ${beside}` : ""}`, aside],
    [sign.compact ? "" : sign.notice || sign.room, sign.lyraMeter ? `LYRA ${sign.lyraMeter} ${sign.lyraName}` : ""],
  ];
  return Math.max(...rows.map(([left, right]) => stringWidth(left) + (left && right ? 1 : 0) + stringWidth(right)));
}

const wide = barSign({ ...base, columns: 124 });
assert.equal(wide.compact, false);
assert.equal(wide.title, "GLITCH CITY // LYRA'S BAR");
assert.deepEqual([wide.phase, wide.clock], ["DEEP NIGHT", "23:07"]);
assert.deepEqual([wide.mark, wide.state, wide.face], ["●", "BAR OPEN", "( -.- )"]);
assert.equal(wide.session, "rainy-thursday");
assert.deepEqual([wide.output, wide.leave], ["output atomic", "/bar leave"]);
assert.equal(wide.room, formatBarRoom(storm));
assert.deepEqual([wide.lyraMeter, wide.lyraName], ["██░", "夜班清水"]);
// Lyra without a glass reads differently from Lyra with an empty one.
const noGlass = barSign({ ...base, lyraGlass: { active: false, name: "", note: "", fill: 0 }, columns: 124 });
assert.deepEqual([noGlass.lyraMeter, noGlass.lyraName], ["···", "water waiting"]);
assert.equal(barSign({ ...base, lyraGlass: { ...base.lyraGlass, fill: 0 }, columns: 124 }).lyraMeter, "░░░");

// The sign shows what is happening: the state, its mark and the cat change together.
const mixing = barSign({ ...base, busy: true, columns: 124 });
assert.deepEqual([mixing.mark, mixing.state, mixing.face], ["◉", "LYRA IS MIXING", "( o.o )"]);
const lost = barSign({ ...base, busy: true, disconnected: true, columns: 124 });
assert.deepEqual([lost.mark, lost.state, lost.face], ["✕", "SIGNAL LOST", "( x.x )"]);

// A notice takes the room's line on a wide sign and the session's place on a narrow one.
const noticed = barSign({ ...base, notice: "GENERATING 12s", columns: 124 });
assert.deepEqual([noticed.notice, noticed.room, noticed.session], ["GENERATING 12s", "", "rainy-thursday"]);
const narrowNotice = barSign({ ...base, notice: "GENERATING 12s", columns: 60 });
assert.deepEqual([narrowNotice.notice, narrowNotice.room, narrowNotice.session], ["GENERATING 12s", "", ""]);

const narrow = barSign({ ...base, columns: 60 });
assert.equal(narrow.compact, true);
assert.equal(narrow.title, "LYRA'S BAR");
assert.deepEqual([narrow.room, narrow.lyraMeter, narrow.lyraName], ["", "", ""]);
assert.equal(narrow.session, "rainy-thursday");
assert.equal(narrow.leave, "/bar leave");

// Whatever the width and however long the names, no row is wider than the frame holds, and
// the way out is the last thing to go.
const long = {
  ...base,
  session: "一个名字特别特别长的雨夜酒吧会话".repeat(2),
  lyraGlass: { active: true, name: "名字很长很长很长的一杯特调".repeat(2), note: "", fill: 3 },
  busy: true,
};
for (const columns of [200, 124, 111, 110, 92, 70, 69, 60, 44, 32, 24]) {
  for (const input of [base, long, { ...long, notice: "正在压缩上下文…这条提示也写得很长很长很长很长" }]) {
    const sign = barSign({ ...input, columns });
    const room = columns - 4 - (sign.compact ? 0 : 9);
    assert.ok(widest(sign) <= room, `${columns} columns: ${widest(sign)} cells in ${room}\n${JSON.stringify(sign)}`);
    if (columns >= 44) assert.equal(sign.leave, "/bar leave", `${columns} columns keeps the way out`);
  }
}
