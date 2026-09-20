import type { RenderLine } from "./markdown-render-lines.js";
import type { StreamingRole } from "./streaming-markdown.js";
import { appendLiveHistory, createLiveHistoryBatchAppender } from "./static-history.js";

export type StreamingDisplayState = {
  historyLines: RenderLine[];
  historyGeneration: number;
  previewLines: Partial<Record<StreamingRole, RenderLine[]>>;
  pendingActivityTail: RenderLine[];
  toolDetailQueue: RenderLine[];
  toolDetailOpen: boolean;
};

export type StreamingDisplayAction =
  | { type: "batch"; actions: StreamingDisplayAction[] }
  | { type: "appendActivity"; lines: RenderLine[] }
  | {
      type: "applyStreamUpdate";
      role: StreamingRole;
      committed: RenderLine[];
      preview: RenderLine[];
    }
  | { type: "setToolDetailOpen"; open: boolean }
  | { type: "hideDynamicTimeRails" }
  | { type: "clearStreaming" }
  | { type: "replaceHistory"; lines: RenderLine[] }
  | { type: "clearHistory" };

export function createStreamingDisplayState(
  historyLines: RenderLine[] = [],
): StreamingDisplayState {
  return {
    historyLines,
    historyGeneration: 0,
    previewLines: {},
    pendingActivityTail: [],
    toolDetailQueue: [],
    toolDetailOpen: false,
  };
}

function hasPreview(previewLines: StreamingDisplayState["previewLines"]): boolean {
  return Object.values(previewLines).some((lines) => Boolean(lines?.length));
}

function appendStaticInput(
  state: StreamingDisplayState,
  lines: RenderLine[],
  append: typeof appendLiveHistory,
): StreamingDisplayState {
  if (lines.length === 0) return state;
  return state.toolDetailOpen
    ? { ...state, toolDetailQueue: append(state.toolDetailQueue, lines) }
    : { ...state, historyLines: append(state.historyLines, lines) };
}

function withoutTimeRails(lines: RenderLine[]): RenderLine[] {
  return lines.map((line) => line.timeRail ? { ...line, timeRail: undefined } : line);
}

export function visibleDynamicDisplayLines(state: StreamingDisplayState, maxLines: number): RenderLine[] {
  let remaining = Number.isFinite(maxLines) ? Math.max(1, Math.floor(maxLines)) : 24;
  // Only window the mutable preview. Completed Markdown still commits in full
  // to Static/terminal scrollback; no canonical text or state is discarded.
  const chunks: RenderLine[][] = [];
  for (const lines of [state.pendingActivityTail, state.previewLines.assistant ?? [], state.previewLines.reasoning ?? []]) {
    if (remaining === 0) break;
    const count = Math.min(remaining, lines.length);
    if (count) chunks.unshift(lines.slice(-count));
    remaining -= count;
  }
  return chunks.flat();
}

export function orderedDynamicDisplayLines(state: StreamingDisplayState): RenderLine[] {
  return [
    ...(state.previewLines.reasoning ?? []),
    ...(state.previewLines.assistant ?? []),
    ...state.pendingActivityTail,
  ];
}

export function streamingDisplayReducer(
  state: StreamingDisplayState,
  action: StreamingDisplayAction,
): StreamingDisplayState {
  return reduceDisplay(state, action, action.type === "batch"
    ? createLiveHistoryBatchAppender()
    : appendLiveHistory);
}

function reduceDisplay(
  state: StreamingDisplayState,
  action: StreamingDisplayAction,
  append: typeof appendLiveHistory,
): StreamingDisplayState {
  if (action.type === "batch") {
    return action.actions.reduce((current, next) => reduceDisplay(current, next, append), state);
  }

  if (action.type === "appendActivity") {
    if (action.lines.length === 0) return state;
    if (hasPreview(state.previewLines)) {
      return {
        ...state,
        pendingActivityTail: append(state.pendingActivityTail, action.lines),
      };
    }
    return appendStaticInput(state, action.lines, append);
  }

  if (action.type === "applyStreamUpdate") {
    const previewLines = { ...state.previewLines };
    if (action.preview.length > 0) {
      previewLines[action.role] = action.preview;
    } else {
      delete previewLines[action.role];
    }

    const previewRemains = hasPreview(previewLines);
    const crossesSafeBoundary = action.committed.length > 0 || !previewRemains;
    const ready = crossesSafeBoundary
      ? [...action.committed, ...state.pendingActivityTail]
      : action.committed;
    const withPreview = {
      ...state,
      previewLines,
      pendingActivityTail: crossesSafeBoundary ? [] : state.pendingActivityTail,
    };
    return appendStaticInput(withPreview, ready, append);
  }

  if (action.type === "setToolDetailOpen") {
    if (action.open === state.toolDetailOpen) return state;
    if (action.open) return { ...state, toolDetailOpen: true };
    return {
      ...state,
      toolDetailOpen: false,
      historyLines: append(state.historyLines, state.toolDetailQueue),
      toolDetailQueue: [],
    };
  }

  if (action.type === "hideDynamicTimeRails") {
    return {
      ...state,
      previewLines: Object.fromEntries(
        Object.entries(state.previewLines).map(([role, lines]) => [
          role,
          withoutTimeRails(lines ?? []),
        ]),
      ),
      pendingActivityTail: withoutTimeRails(state.pendingActivityTail),
      toolDetailQueue: withoutTimeRails(state.toolDetailQueue),
    };
  }

  if (action.type === "clearStreaming") {
    return {
      ...state,
      previewLines: {},
      pendingActivityTail: [],
    };
  }

  if (action.type === "replaceHistory") {
    return {
      ...state,
      historyLines: action.lines,
      // Reset Ink's Static cursor in the same render as its replacement items.
      // Separate updates first print a suffix with the old cursor, then replay it.
      historyGeneration: state.historyGeneration + 1,
      previewLines: {},
      pendingActivityTail: [],
      toolDetailQueue: [],
    };
  }

  return {
    ...state,
    historyLines: [],
    historyGeneration: state.historyGeneration + 1,
    previewLines: {},
    pendingActivityTail: [],
    toolDetailQueue: [],
    toolDetailOpen: false,
  };
}

// Coalesce chunks delivered in the same event-loop turn. End-of-stream and
// control transitions use dispatch(), which flushes synchronously so a final
// commit, reset, or activity cannot overtake an earlier preview update.
export function createStreamingDisplayBatcher(
  publish: (action: StreamingDisplayAction) => void,
) {
  let pending: StreamingDisplayAction[] = [];
  const flush = () => {
    if (pending.length === 0) return;
    const actions = pending;
    pending = [];
    publish(actions.length === 1 ? actions[0] : { type: "batch", actions });
  };
  return {
    enqueue(action: StreamingDisplayAction) {
      pending.push(action);
      if (pending.length === 1) queueMicrotask(flush);
    },
    dispatch(action: StreamingDisplayAction) {
      flush();
      publish(action);
    },
    flush,
  };
}
