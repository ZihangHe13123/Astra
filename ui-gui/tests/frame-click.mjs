import { expect } from '@playwright/test';

/**
 * Click something inside a card or page frame, which runs in a process of its own. For a moment after such a frame
 * appears, the browser still hands the pointer to the frame element in the host page, not to the document inside:
 * a click then is lost, although Playwright's own check, made inside the frame, saw the target under the pointer
 * (macOS runner 2026-10-25, a counter card clicked as it came back into view stayed at 0; locally 1 click in 40-60
 * right after a card appeared, delivered to the host's iframe at the button's position). The pointer is moved onto
 * the target until the frame itself has it under the pointer, and only then pressed.
 */
export async function clickInFrame(target) {
  await expect.poll(async () => { await target.hover(); return target.evaluate(node => node.matches(':hover')); }, { intervals: [0, 50, 100, 250] }).toBe(true);
  await target.click();
}
