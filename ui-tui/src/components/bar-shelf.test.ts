import assert from "node:assert/strict";
import stringWidth from "string-width";
import { barDrinkCard, barShelfLayout, drinkFillMeter, drinkGlass, drinkTemperatureLabel, NO_GLASS } from "./bar-shelf.js";
import type { BarDrinkState } from "../types.js";

assert.equal(drinkFillMeter(3), "███");
assert.equal(drinkFillMeter(2), "██░");
assert.equal(drinkFillMeter(1), "█░░");
assert.equal(drinkFillMeter(0), "░░░");
assert.equal(drinkFillMeter(99), "███");
assert.equal(drinkFillMeter(-5), "░░░");
// An empty glass and no glass read differently, at the same width.
assert.notEqual(NO_GLASS, drinkFillMeter(0));
assert.equal(stringWidth(NO_GLASS), stringWidth(drinkFillMeter(0)));
assert.equal(barShelfLayout({ active: false, name: "", note: "", tone: "clear", temperature: "room", fill: 0 }, 100).prefix, `${NO_GLASS} GLASS WAITING`);
assert.equal(drinkTemperatureLabel("hot"), "HOT");
assert.equal(drinkTemperatureLabel("cold"), "COLD");
assert.equal(drinkTemperatureLabel("room"), "ROOM TEMP");

const longDrink: BarDrinkState = {
  active: true,
  name: "系统过载但名称也故意写得非常非常长".repeat(2),
  note: "高浓度的龙舌兰混入深色苦艾酒，带着一丝冷冽的薄荷香气，入口像是一次强制重启".repeat(2),
  tone: "cyan",
  temperature: "cold",
  fill: 2,
};
const wide = barShelfLayout(longDrink, 100);
assert.equal(wide.compact, false);
assert.equal(wide.hint, "/sip to drink");
assert.equal(wide.prefix.endsWith("…"), true);
assert.equal(wide.note.endsWith("…"), true);
assert.equal(
  stringWidth(`BAR SHELF // ${wide.prefix}${wide.spacer}${wide.hint}`),
  95,
);
assert.ok(stringWidth(`TASTING // ${wide.note}`) <= 95);

const compact = barShelfLayout(longDrink, 60);
assert.equal(compact.compact, true);
assert.equal(compact.hint, "/sip");
assert.equal(compact.note, "");
assert.equal(
  stringWidth(`SHELF // ${compact.prefix}${compact.spacer}${compact.hint}`),
  55,
);

// A poured drink is drawn as a glass. Whatever is in it, the drawing keeps its size, so the
// text beside it never moves.
const cells = (glass: ReturnType<typeof drinkGlass>, part: string) =>
  glass.flat().filter((piece) => piece.part === part).map((piece) => piece.text.replaceAll(" ", "")).join("");
for (const temperature of ["hot", "cold", "room"] as const) {
  const liquid: number[] = [];
  for (const fill of [3, 2, 1, 0]) {
    const glass = drinkGlass({ temperature, fill });
    assert.equal(glass.length, 4);
    for (const row of glass) assert.equal(stringWidth(row.map((piece) => piece.text).join("")), 9, `${temperature} ${fill}`);
    // How much is drawn, in eighths of a cell: a full block is eight, the lower blocks less.
    liquid.push([...cells(glass, "liquid")].reduce((sum, block) => sum + "▁▂▃▄▅▆▇█".indexOf(block) + 1, 0));
    // Ice is what a cold drink has, and what it leaves; steam rises only from something hot.
    assert.equal(cells(glass, "ice").length > 0, temperature === "cold", `${temperature} ${fill}: ice`);
    assert.equal(cells(glass, "steam").length > 0, temperature === "hot" && fill > 0, `${temperature} ${fill}: steam`);
  }
  // Every sip leaves less in the glass, and the last one leaves none.
  assert.ok(liquid[0] > liquid[1] && liquid[1] > liquid[2] && liquid[2] > liquid[3], `${temperature}: ${liquid}`);
  assert.equal(liquid[3], 0);
}
assert.notDeepEqual(drinkGlass({ temperature: "hot", fill: 2 }), drinkGlass({ temperature: "room", fill: 2 }));
assert.deepEqual(drinkGlass({ temperature: "room", fill: 99 }), drinkGlass({ temperature: "room", fill: 3 }));

const sugar: BarDrinkState = { active: true, name: "Sugar Rush", note: "甜，带一点电流感，后味是青柠。", tone: "pink", temperature: "cold", fill: 2 };
const card = barDrinkCard(sugar, 100);
assert.equal(card.name, "Sugar Rush");
assert.equal(card.temperature, "COLD");
assert.equal(card.sips, "two sips left");
assert.equal(card.hint, "/sip to drink");
assert.deepEqual(card.note, ["甜，带一点电流感，后味是青柠。"]);
// Frame and padding take four cells, the glass and its gap ten.
assert.equal(stringWidth(`${card.name}${card.spacer}${card.hint}`), 100 - 4 - 10);
// An empty glass says so once, and offers no sip.
assert.deepEqual(
  (({ sips, hint }) => ({ sips, hint }))(barDrinkCard({ ...sugar, fill: 0 }, 100)),
  { sips: "empty", hint: "" },
);

// A long note takes two rows and no more: it is told whole, or as far as it goes with a mark
// that it goes on. Nothing is wider than the card, a row does not open with closing
// punctuation, and words stay whole.
for (const columns of [70, 84, 100, 160]) {
  const room = columns - 4 - 10;
  const long = barDrinkCard(longDrink, columns);
  assert.equal(long.note.length, 2);
  const told = long.note.join("");
  assert.ok(
    told === longDrink.note || (told.endsWith("…") && longDrink.note.startsWith(told.slice(0, -1))),
    `${columns}: ${told}`,
  );
  assert.ok(long.name === longDrink.name || long.name.endsWith("…"), `${columns}: ${long.name}`);
  for (const row of [...long.note, `${long.name}${long.spacer}${long.hint}`]) assert.ok(stringWidth(row) <= room, `${columns}: ${row}`);
  for (const row of long.note) assert.doesNotMatch(row, /^[，。、；：！？,.;:!?]/u, `${columns}: ${row}`);
}
assert.deepEqual(
  barDrinkCard({ ...sugar, note: "Clear, dry and a little too honest for this hour of the night." }, 72).note,
  ["Clear, dry and a little too honest for this hour of the", "night."],
);
