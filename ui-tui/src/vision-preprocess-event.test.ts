import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { formatVisionPreprocessMessage } from "./app.js";
import type { VisionPreprocessEvent } from "./types.js";

const appSource = readFileSync(new URL("./app.tsx", import.meta.url), "utf8");
const typesSource = readFileSync(new URL("../../ui-core/src/types.ts", import.meta.url), "utf8");

assert.match(typesSource, /type: "vision_preprocess"/);
assert.match(appSource, /case "vision_preprocess"/);

const visionCaseStart = appSource.indexOf('case "vision_preprocess"');
const visionCaseEnd = appSource.indexOf("case ", visionCaseStart + 1);
const visionCase = appSource.slice(visionCaseStart, visionCaseEnd);

assert.match(visionCase, /formatVisionPreprocessMessage\(event\)/);

const liveEvent: VisionPreprocessEvent = {
  type: "vision_preprocess",
  message: "Prepared 1 protected local image with 8 original-pixel detail tiles.",
  protected: true,
  protected_local_images: 1,
  unprotected_external_images: 0,
};
const replayedEvent: VisionPreprocessEvent & { replayed: true } = {
  ...liveEvent,
  replayed: true,
};

assert.equal(
  formatVisionPreprocessMessage(liveEvent),
  "[vision] Prepared 1 protected local image with 8 original-pixel detail tiles.",
);
assert.equal(
  formatVisionPreprocessMessage(replayedEvent),
  "[vision] Prepared 1 protected local image with 8 original-pixel detail tiles.",
);
assert.equal(formatVisionPreprocessMessage({ ...liveEvent, message: "" }), null);
