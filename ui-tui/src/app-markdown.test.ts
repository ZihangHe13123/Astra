import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import stringWidth from "string-width";
import {
  MESSAGE_TIME_RAIL_WIDTH,
  formatQuestionAnswerDisplaySummary,
  messageRolePrefixWidth,
  messageToLines,
  shouldRefreshToolClock,
  shouldShowWelcome,
} from "./app.js";
import type { ChatMessage, UserQuestionAnswer, UserQuestionRequest } from "./types.js";

const idleWelcomeState = {
  backendStatus: "ready" as const,
  runtimeMode: "work" as const,
  historyLineCount: 0,
  openToolResultId: null,
  busy: false,
  activeTask: false,
  activeToolCount: 0,
  activeProcessCount: 0,
  approvalCount: 0,
  commandMenuVisible: false,
};

assert.equal(shouldShowWelcome(idleWelcomeState), true);
assert.equal(shouldShowWelcome({ ...idleWelcomeState, commandMenuVisible: true }), false);
assert.equal(shouldShowWelcome({ ...idleWelcomeState, runtimeMode: "minimal" }), false);
assert.equal(shouldShowWelcome({ ...idleWelcomeState, runtimeMode: "local" }), false);
assert.equal(shouldShowWelcome({ ...idleWelcomeState, approvalCount: 1 }), false);
assert.equal(shouldShowWelcome({ ...idleWelcomeState, activeToolCount: 1 }), false);
assert.equal(shouldRefreshToolClock({ activeToolCount: 1, activeProcessCount: 0, approvalCount: 0 }), true);
assert.equal(shouldRefreshToolClock({ activeToolCount: 1, activeProcessCount: 0, approvalCount: 1 }), false);

const restoredMessage: ChatMessage = {
  role: "assistant",
  content: String.raw`之前的文字

$$
c_{\text{rest}} = \frac{1}{c_{\text{intensity}}} \Rightarrow \text{失眠状态}
$$

$$
\boxed{\text{失眠}\gg)
$$`,
  timestamp: 1,
};

const lines = messageToLines(restoredMessage, 120, true);
const mathLines = lines.filter((line) => line.kind === "math");

assert.equal(lines.some((line) => line.text.trim() === "$$"), false);
assert.equal(mathLines.length, 2);
assert.ok(mathLines[0].text.includes("cᵣₑₛₜ = (1)/(c_(intensity)) ⇒ 失眠状态"));
assert.equal(mathLines[0].malformedMath, false);
assert.equal(mathLines[1].malformedMath, true);

const minimalLines = messageToLines(
  { role: "assistant", content: "response", timestamp: 2 },
  120,
  true,
  "minimal",
);
assert.equal(minimalLines[0].prefix, "assistant ");

const fixedTimestamp = new Date(2025, 5, 15, 17, 6, 40).getTime();
const timestampedLines = messageToLines(
  { role: "user", content: "first\nsecond", timestamp: fixedTimestamp },
  120,
  true,
  "work",
  { label: "17:06", kind: "absolute", width: 8, prefixWidth: 7 },
);
assert.deepEqual(timestampedLines[0].timeRail, { label: "17:06", kind: "absolute", width: 8, prefixWidth: 7 });
assert.equal(
  timestampedLines.filter((line) => line.role === "user").slice(1).every((line) => line.timeRail?.label === ""),
  true,
);
const wrappedTimestampedLines = messageToLines(
  { role: "assistant", content: "x".repeat(80), timestamp: fixedTimestamp },
  40,
  true,
  "work",
  { label: "17:06", kind: "absolute", width: 8, prefixWidth: 7 },
);
const wrappedConversationLines = wrappedTimestampedLines.filter((line) => line.role === "assistant");
assert.ok(wrappedConversationLines.length > 1);
assert.equal(wrappedConversationLines[0].timeRail?.label, "17:06");
assert.equal(wrappedConversationLines.slice(1).every((line) => line.timeRail?.label === ""), true);
assert.equal(wrappedTimestampedLines.some((line) => line.role === "system" && line.timeRail), false);

const restoredLeak = messageToLines(
  {
    role: "assistant",
    content: "<message_time>2026-08-24T20:43:36+08:00</message_time>\nclean",
  },
  120,
  true,
  "work",
);
assert.equal(restoredLeak.some((line) => line.text.includes("message_time")), false);
assert.equal(restoredLeak.some((line) => line.text.includes("clean")), true);

const inlineMarker = messageToLines(
  { role: "assistant", content: "quote <message_time>example</message_time>" },
  120,
  true,
  "work",
);
assert.equal(inlineMarker.some((line) => line.text.includes("message_time")), true);

const timestampedToolLines = messageToLines(
  { role: "tool", content: "result", timestamp: fixedTimestamp },
  120,
  true,
);
assert.equal(timestampedToolLines.some((line) => line.timeRail), false);

const unrailedTool = messageToLines(
  { role: "tool", content: "result", timestamp: fixedTimestamp },
  40,
  true,
  "work",
);
assert.equal(unrailedTool.some((line) => line.timeRail), false);

const explicitToolRail = {
  label: "20:43",
  kind: "absolute" as const,
  width: MESSAGE_TIME_RAIL_WIDTH,
  prefixWidth: messageRolePrefixWidth("tool", "work"),
};
const railedTool = messageToLines(
  { role: "tool", content: "✓ execute_shell 3233ms", timestamp: fixedTimestamp },
  40,
  true,
  "work",
  explicitToolRail,
);
assert.equal(railedTool.some((line) => line.timeRail), true);

const screenshotMarkdown = messageToLines(
  {
    role: "assistant",
    content: [
      "   ### P21: Cascade 级联",
      "系统会自动**更信任平时表现好的推荐器**，并淘汰~~旧策略~~。",
      "```python",
      "CF coarse → DNN rerank → Top N",
      "```",
    ].join("\n"),
    timestamp: fixedTimestamp,
  },
  44,
  true,
  "work",
  { label: "09:44", kind: "absolute", width: MESSAGE_TIME_RAIL_WIDTH, prefixWidth: 6 },
);
assert.equal(screenshotMarkdown.some((line) => line.text.includes("**")), false);
assert.equal(screenshotMarkdown.some((line) => line.text.includes("~~")), false);
assert.equal(screenshotMarkdown.some((line) => line.text.includes("```")), false);
assert.equal(screenshotMarkdown.some((line) => line.text.includes("python")), false);
assert.equal(screenshotMarkdown.some((line) => line.text === "code" || line.text === "end code"), false);
assert.equal(screenshotMarkdown.find((line) => line.kind === "header")?.text, "P21: Cascade 级联");
const screenshotCodeLines = screenshotMarkdown.filter((line) => line.kind === "code");
assert.equal(screenshotCodeLines.every((line) => line.text.length > 0), true);
assert.equal(screenshotCodeLines.map((line) => line.text).join(""), "CF coarse → DNN rerank → Top N");
assert.equal(screenshotCodeLines.every((line) =>
  (line.timeRail?.prefixWidth ?? stringWidth(line.prefix))
    + stringWidth(line.text)
    + (line.timeRail?.width ?? 0)
    <= Math.max(30, 44 - 8)
), true);
const screenshotParagraphLines = screenshotMarkdown.filter((line) => line.role === "assistant" && line.kind === "text");
assert.equal(screenshotParagraphLines.map((line) => line.text).join(""), "系统会自动更信任平时表现好的推荐器，并淘汰旧策略。");
const recommendationSpans = screenshotParagraphLines.flatMap((line) => line.spans ?? []).filter((span) => span.text.includes("推荐器"));
assert.ok(recommendationSpans.length > 0);
assert.equal(recommendationSpans.every((span) => span.kind === "strong"), true);
const retiredSpans = screenshotParagraphLines
  .flatMap((line) => line.spans ?? [])
  .filter((span) => span.kind === "strikethrough");
assert.equal(retiredSpans.map((span) => span.text).join(""), "旧策略");

const hostileQuestionRequest: UserQuestionRequest = {
  type: "user_question_request",
  request_id: "hostile-summary",
  questions: [{
    id: "storage\u200b",
    header: "Storage\u2066\u{E0020}\x1b",
    question: "Choose storage",
    options: [{ label: "SQLite\u202e\u{E0020}" }],
    multi_select: false,
  }, {
    id: "notes\u200b",
    question: "Any constraints?",
    multi_select: false,
  }],
};
const hostileAnswers: UserQuestionAnswer[] = [{
  id: "storage\u200b",
  selected: ["SQLite\u202e\u{E0020}"],
}, {
  id: "notes\u200b",
  selected: [],
  custom: "Keep bidi\u2067 isolated",
}];
const unchangedHostileAnswers = structuredClone(hostileAnswers);
const hostileSummary = formatQuestionAnswerDisplaySummary(hostileQuestionRequest, hostileAnswers);
assert.doesNotMatch(hostileSummary, /[\x1b\u200b\u202e\u2066\u{E0020}]/u);
assert.match(hostileSummary, /\\x1b/);
assert.match(hostileSummary, /\\u\{200B\}/);
assert.match(hostileSummary, /\\u\{202E\}/);
assert.match(hostileSummary, /\\u\{2066\}/);
assert.match(hostileSummary, /\\u\{E0020\}/);
assert.deepEqual(hostileAnswers, unchangedHostileAnswers);

const appSource = readFileSync(new URL("./app.tsx", import.meta.url), "utf8");
const typesSource = readFileSync(new URL("../../ui-core/src/types.ts", import.meta.url), "utf8");
const appModule = await import("./app.js");
const submitStreamLifecyclePolicy = (appModule as unknown as Record<string, unknown>)
  .submitStreamLifecyclePolicy;
assert.equal(typeof submitStreamLifecyclePolicy, "function");
for (const { input, expected } of [
  { input: "/cancel", expected: "preserve" },
  { input: "/theme glitchcity", expected: "preserve" },
  { input: "/timeline off", expected: "preserve" },
  { input: "/reset", expected: "clear" },
  { input: "/reconnect", expected: "clear" },
  { input: "normal user input", expected: "clear" },
  { input: "/session accepted-session", expected: "clear" },
  { input: "/bar accepted-session", expected: "clear" },
  { input: "/minimal accepted-session", expected: "clear" },
]) {
  assert.equal(
    (submitStreamLifecyclePolicy as (text: string) => string)(input),
    expected,
    input,
  );
}
assert.match(typesSource, /type: "history"; messages: \{ role: string; content: string; timestamp\?: number \}\[\]/);
assert.match(appSource, /m\.timestamp \* 1000/);
assert.match(appSource, /options\.timestamp \?\? Date\.now\(\)/);
assert.match(appSource, /timeRailCursorRef/);
assert.match(appSource, /useState\(loadTimelineDisplay\)/u);
assert.match(appSource, /resolveTimelineCommand\(text, timelineVisibleRef\.current\)/u);
assert.match(appSource, /saveTimelineDisplay\(timelineCommand\.enabled\)/u);
assert.match(appSource, /DISPLAY  \/theme  \/timeline/u);
assert.match(appSource, /toolCompletion: true, timestamp: completedAt/u);
assert.match(appSource, /const completedAt = Date\.now\(\)/u);
assert.match(appSource, /timeRailCursorRef\.current\.reset\(\)/);
assert.match(appSource, /theme\.chrome\?\.separator/);
assert.match(
  appSource,
  /m\.role === "user" \|\| m\.role === "reasoning" \|\| m\.role === "assistant"/,
);
assert.match(appSource, /railLayout\(role\)/);
assert.match(appSource, /new StreamingMarkdownCoordinator\(\)/u);
assert.match(appSource, /streamingDisplayReducer/u);
assert.match(appSource, /createStreamingDisplayState/u);
assert.match(appSource, /dispatchDisplay/u);
assert.match(appSource, /applyStreamingUpdate/u);
assert.match(
  appSource,
  /const previewStartingLineIndex = update\.startingLineIndex \+ committed\.consumedLines/u,
);
assert.match(appSource, /streamCoordinatorRef\.current\.advance\(role, committed\.consumedLines\)/u);
assert.match(appSource, /applyStreamingUpdate\(role, update, false\)/u);
assert.match(appSource, /streamCoordinatorRef\.current\.clearTimeRails\(\)/u);
assert.match(appSource, /type: "hideDynamicTimeRails"/u);
assert.doesNotMatch(appSource, /setStreamPreviewLines|setHistoryLines|pendingHistoryLinesRef/u);
assert.doesNotMatch(appSource, /state\.partial\.includes\("\\n"\)/u);
assert.doesNotMatch(appSource, /commitStreamLine/u);
assert.doesNotMatch(appSource, /streamingLineWidth/u);
assert.doesNotMatch(appSource, /LegacyInlineSegment|parseLegacyInlineMarkdown|LegacyInlineMarkdown/u);
assert.doesNotMatch(appSource, /pendingMath|pendingTable|mathDelimiter/u);
const mainBoxIndex = appSource.indexOf('<Box\n      flexDirection="column"');
const staticIndex = appSource.indexOf("<HistoryOutput", mainBoxIndex);
const layoutIndex = appSource.indexOf("<StreamingControlLayout", staticIndex);
const previewIndex = appSource.indexOf("dynamic={", layoutIndex);
const headerIndex = appSource.indexOf("header={", previewIndex);
const statusIndex = appSource.indexOf("status={", headerIndex);
const auxiliaryIndex = appSource.indexOf("auxiliary={", statusIndex);
const inputIndex = appSource.indexOf("input={", auxiliaryIndex);
assert.ok(
  mainBoxIndex >= 0
    && staticIndex > mainBoxIndex
    && layoutIndex > staticIndex
    && previewIndex > layoutIndex
    && headerIndex > previewIndex
    && statusIndex > headerIndex
    && auxiliaryIndex > statusIndex
    && inputIndex > auxiliaryIndex,
);
assert.equal((appSource.match(/<BarHeaderMemo\b/g) ?? []).length, 1);
assert.equal((appSource.match(/<AppHeaderMemo\b/g) ?? []).length, 1);
const completeTurnSource = appSource.slice(
  appSource.indexOf("const completeTurn"),
  appSource.indexOf("const handleEvent"),
);
const callCount = (source: string, call: string) => source.split(call).length - 1;
const reasoningFlush = 'flushStreamRole("reasoning")';
const assistantFlush = 'flushStreamRole("assistant")';
assert.equal(callCount(completeTurnSource, reasoningFlush), 1);
assert.equal(callCount(completeTurnSource, assistantFlush), 1);
assert.ok(completeTurnSource.indexOf(reasoningFlush) < completeTurnSource.indexOf(assistantFlush));
assert.match(appSource, /streamCoordinatorRef\.current\.clear\(\)/u);
assert.match(appSource, /dispatchDisplay\(\{ type: "clearStreaming" \}\)/u);
assert.match(appSource, /const clearStreamingDisplay = useCallback/u);
assert.match(appSource, /export function streamingUpdateToRenderLines/u);
const historyCase = appSource.slice(
  appSource.indexOf('case "history"'),
  appSource.indexOf('case "working_memory"'),
);
assert.match(historyCase, /clearStreamingDisplay\(\)/u);
const timelineBranchStart = appSource.indexOf(
  "if (timelineCommand)",
  appSource.indexOf('if (text.startsWith("/"))'),
);
const timelineBranch = appSource.slice(
  timelineBranchStart,
  appSource.indexOf('} else if (text === "/reset")'),
);
assert.match(timelineBranch, /timeRailCursorRef\.current\.reset\(\)/u);
assert.match(
  timelineBranch,
  /shouldResetTimelineCursor\(\s*timelineVisibleRef\.current,\s*timelineCommand\.enabled,?\s*\)/u,
);
assert.doesNotMatch(timelineBranch, /\bsend\(/u);
assert.doesNotMatch(timelineBranch, /clearStreamingDisplay|completeTurn|flushStreamRole/u);
const themeBranch = appSource.slice(
  appSource.indexOf("if (themeMatch)"),
  appSource.indexOf("if (timelineCommand)"),
);
assert.doesNotMatch(themeBranch, /clearStreamingDisplay|completeTurn|flushStreamRole/u);
const submitLifecycleDecision = appSource.slice(
  appSource.indexOf("const timelineCommand"),
  appSource.indexOf("const image = parseImageInput"),
);
const submitSource = appSource.slice(
  appSource.indexOf("const submit = useCallback"),
  appSource.indexOf("const openToolResult ="),
);
assert.match(
  submitLifecycleDecision,
  /const streamLifecycle = submitStreamLifecyclePolicy\(text, timelineCommand\)/u,
);
assert.match(
  submitLifecycleDecision,
  /if \(streamLifecycle === "clear"\) clearStreamingDisplay\(\)/u,
);
assert.equal(callCount(submitSource, "clearStreamingDisplay();"), 1);
const toolResultCase = appSource.slice(
  appSource.indexOf('case "tool_result"'),
  appSource.indexOf('case "done"'),
);
assert.match(toolResultCase, /toolCompletion: true/u);
assert.equal(callCount(toolResultCase, assistantFlush), 1);
const toolCompletedAt = toolResultCase.indexOf("const completedAt = Date.now()");
const toolAssistantFlush = toolResultCase.indexOf(assistantFlush);
const toolOutputAllocation = toolResultCase.indexOf('addMessage(\n            "tool"');
assert.ok(toolCompletedAt >= 0 && toolCompletedAt < toolAssistantFlush);
assert.ok(toolAssistantFlush < toolOutputAllocation);
const ctrlCBranch = appSource.slice(
  appSource.indexOf('if (key.ctrl && input === "c")'),
  appSource.indexOf("if (approvalRequests.length > 0)"),
);
assert.match(ctrlCBranch, /addMessage\("system", "Cancelling active task/u);
assert.doesNotMatch(ctrlCBranch, /clearStreamingDisplay|completeTurn|flushStreamRole/u);
const recoverableErrorCase = appSource.slice(
  appSource.indexOf("if (event.recoverable)"),
  appSource.indexOf("finishTurn();", appSource.indexOf("if (event.recoverable)")),
);
assert.match(recoverableErrorCase, /addMessage\("tool"/u);
assert.doesNotMatch(recoverableErrorCase, /toolCompletion/u);
assert.match(appSource, /case "user_question_request"/);
assert.match(appSource, /case "user_question_resolved"/);
assert.match(appSource, /case "user_question_response_rejected"/);
assert.match(appSource, /reduceQuestionProtocolState/);
assert.match(appSource, /<QuestionCard/);
assert.match(appSource, /submissionRejection=\{questionProtocol\.rejection\}/);
assert.match(appSource, /active=\{!openToolResult && approvalRequests\.length === 0\}/);
assert.match(appSource, /type: "user_question_response"/);
assert.match(appSource, /type: "user_question_cancel"/);
assert.match(appSource, /addMessage\("user", formatQuestionAnswerDisplaySummary\(questionRequest, answers\)\)/);
assert.match(appSource, /disabled=\{connectionOpen \|\| Boolean\(openToolResult\) \|\| approvalRequests\.length > 0 \|\| questionRequest !== null\}/);
assert.match(appSource, /if \(questionRequest\) return;/);
assert.doesNotMatch(appSource, /submit\(formatQuestionAnswerSummary/);
assert.match(appSource, /queueIndex=\{1\}/);
assert.match(appSource, /queueTotal=\{approvalRequests\.length\}/);

const chunkCase = appSource.slice(appSource.indexOf('case "chunk"'), appSource.indexOf('case "reasoning"'));
assert.equal(callCount(chunkCase, reasoningFlush), 1);
assert.equal(callCount(chunkCase, assistantFlush), 0);
assert.ok(chunkCase.indexOf(reasoningFlush) < chunkCase.indexOf("assistantStartedRef.current = true"));
// Completion, failure and backend-exit cleanup are exercised through real Ink
// events in app-lifecycle-render.test.tsx, including exceptions while flushing.
