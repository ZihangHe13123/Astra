import React, { useEffect, useState } from "react";
import { Box, Text, useInput } from "ink";
import TextInput from "ink-text-input";
import stringWidth from "string-width";
import { graphemeBoundary } from "../grapheme-editing.js";
import { escapeApprovalDisplayText } from "../approval-preview.js";
import { singleLineFieldText } from "../paste-command.js";
import {
  createQuestionState,
  transitionQuestion,
  type QuestionAction,
  type QuestionTransition,
} from "../question-interaction.js";
import type { UserQuestionAnswer, UserQuestionRequest } from "../types.js";
import { wrapToolResult } from "../tool-results.js";
import { useTheme } from "../theme-context.js";

export interface QuestionCardProps {
  request: UserQuestionRequest;
  active: boolean;
  width: number;
  maxHeight?: number;
  onAnswer: (requestId: string, answers: UserQuestionAnswer[]) => void;
  onCancel: (requestId: string) => void;
  onEditing?: (requestId: string, editing: boolean) => void;
  submissionRejection?: { requestId: string; reason: string } | null;
}

function sanitizeDisplay(value: string): string {
  return escapeApprovalDisplayText(value).replace(/\s+/g, " ").trim();
}

function truncateDisplay(value: string, maxWidth: number): string {
  const safe = sanitizeDisplay(value);
  if (stringWidth(safe) <= maxWidth) return safe;
  if (maxWidth <= 1) return "…";
  let output = "";
  for (const character of safe) {
    if (stringWidth(output + character) >= maxWidth) break;
    output += character;
  }
  return `${output}…`;
}

export function QuestionCard({
  request,
  active,
  width,
  maxHeight,
  onAnswer,
  onCancel,
  onEditing,
  submissionRejection = null,
}: QuestionCardProps) {
  const theme = useTheme();
  const [state, setState] = useState(() => createQuestionState(request));
  const [optionIndex, setOptionIndex] = useState(0);
  const [customEntry, setCustomEntry] = useState(false);
  const [customText, setCustomText] = useState("");
  const [customCursor, setCustomCursor] = useState(0);
  const [scrollOffset, setScrollOffset] = useState(0);
  const [pending, setPending] = useState<"answer" | "cancel" | null>(null);

  useEffect(() => {
    setState(createQuestionState(request));
    setOptionIndex(0);
    setCustomEntry(false);
    setCustomText("");
    setPending(null);
  }, [request.request_id]);

  useEffect(() => {
    if (submissionRejection?.requestId !== request.request_id) return;
    setPending(null);
    setState((current) => transitionQuestion(current, {
      type: "submission_rejected",
      reason: submissionRejection.reason,
    }).state);
  }, [request.request_id, submissionRejection]);

  useEffect(() => {
    if (request.mode !== "timed" || !onEditing || !customEntry || !active || pending) return;
    onEditing(request.request_id, true);
    const heartbeat = setInterval(() => onEditing(request.request_id, true), 20_000);
    return () => { clearInterval(heartbeat); onEditing(request.request_id, false); };
  }, [request.request_id, request.mode, customEntry, active, pending, onEditing]);

  const question = state.request.questions[state.index];
  const draft = state.drafts[state.index];
  const options = question?.options ?? [];
  const panelWidth = Math.max(20, width);
  const contentWidth = Math.max(16, panelWidth - 4);

  const apply = (...actions: QuestionAction[]): QuestionTransition => {
    if (request.mode === "timed") onEditing?.(request.request_id, true);
    let transition: QuestionTransition = { state };
    for (const action of actions) {
      transition = transitionQuestion(transition.state, action);
    }
    setState(transition.state);
    if (transition.response) {
      setPending("answer");
      onAnswer(transition.response.requestId, transition.response.answers);
    }
    if (transition.cancellation) {
      setPending("cancel");
      onCancel(transition.cancellation.requestId);
    }
    return transition;
  };

  useInput((input, key) => {
    if (key.ctrl || key.tab || (key.meta && !key.escape)) return;
    if (maxHeight !== undefined && (key.pageUp || key.pageDown)) {
      setScrollOffset((current) => Math.max(0, Math.min(maxScrollOffset,
        current + (key.pageUp ? -bodyPageSize : bodyPageSize))));
      return;
    }
    const normalized = input.toLowerCase();
    if (key.upArrow && options.length > 0) {
      focusOption(Math.max(0, optionIndex - 1));
      return;
    }
    if (key.downArrow && options.length > 0) {
      focusOption(Math.min(options.length - 1, optionIndex + 1));
      return;
    }
    if (key.leftArrow) {
      apply({ type: "previous" });
      setOptionIndex(0);
      return;
    }
    if (key.rightArrow) {
      apply({ type: "next" });
      setOptionIndex(0);
      return;
    }
    if (/^[1-9]$/.test(input)) {
      const selected = options[Number(input) - 1];
      if (selected) {
        apply({ type: "toggle", label: selected.label });
        focusOption(Number(input) - 1);
      }
      return;
    }
    if (input === " " && options[optionIndex]) {
      apply({ type: "toggle", label: options[optionIndex].label });
      return;
    }
    if (normalized === "c") {
      setCustomText(draft?.custom ?? "");
      setCustomCursor((draft?.custom ?? "").length);
      setCustomEntry(true);
      return;
    }
    if (normalized === "s") {
      apply({ type: "skip" });
      setOptionIndex(0);
      return;
    }
    if (key.escape) {
      apply({ type: "cancel" });
      return;
    }
    if (key.return) {
      const continuation: QuestionAction = state.index < state.request.questions.length - 1
        ? { type: "next_if_complete" }
        : { type: "submit" };
      const highlighted = options[optionIndex];
      const shouldSelectHighlighted = (
        question?.multi_select === false
        && highlighted !== undefined
        && !draft?.custom
        && !draft?.skipped
      );

      const transition = shouldSelectHighlighted
        ? apply(
          { type: "select", label: highlighted.label },
          continuation,
        )
        : apply(continuation);
      if (transition.state.index !== state.index) setOptionIndex(0);
    }
  }, { isActive: active && !customEntry && pending === null });

  useInput((_input, key) => {
    if (key.ctrl || !key.escape) return;
    apply({ type: "cancel" });
  }, { isActive: active && customEntry && pending === null && maxHeight === undefined });

  useInput((input, key) => {
    if (key.ctrl || key.tab || (key.meta && !key.escape)) return;
    if (key.escape) { apply({ type: "cancel" }); return; }
    if (key.return) { commitCustom(customText); return; }
    if (key.pageUp || key.pageDown) {
      setScrollOffset((current) => Math.max(0, Math.min(maxScrollOffset,
        current + (key.pageUp ? -bodyPageSize : bodyPageSize))));
      return;
    }
    if (key.leftArrow || key.rightArrow) {
      setCustomCursor(graphemeBoundary(customText, customCursor, key.leftArrow ? -1 : 1));
      return;
    }
    if (key.backspace || key.delete) {
      const previous = graphemeBoundary(customText, customCursor, -1);
      setCustomText(customText.slice(0, previous) + customText.slice(customCursor));
      setCustomCursor(previous);
      return;
    }
    if (key.upArrow || key.downArrow || key.tab) return;
    // Clean everything up to the cursor so paste markers never count as answer text.
    const head = singleLineFieldText(customText.slice(0, customCursor) + input, " ");
    setCustomText(head + customText.slice(customCursor));
    setCustomCursor(head.length);
  }, { isActive: active && customEntry && pending === null && maxHeight !== undefined });

  const commitCustom = (value: string) => {
    const transition = transitionQuestion(state, { type: "commit_custom", text: value });
    setState(transition.state);
    if (!transition.state.error) {
      setCustomEntry(false);
      setCustomText(transition.state.drafts[transition.state.index]?.custom ?? "");
    }
  };

  const interactionHint = question?.multi_select
    ? "↑/↓ choose · Space toggle · Enter next/submit · C custom · S skip · Esc cancel"
    : options.length > 0
      ? "↑/↓ choose · Enter select/next/submit · Space select · C custom · S skip · Esc cancel"
      : "Enter next/submit · C custom · S skip · Esc cancel";

  const footer = pending === "answer"
    ? "Submitting answer…"
    : pending === "cancel"
      ? "Cancelling question…"
      : !active
        ? request.state === "pending" ? "Tab to answer · work continues" : "Question paused while approval is active"
        : customEntry
          ? "Enter save custom answer"
          : interactionHint;

  // The bounded view keeps every body line reachable instead of clipping option text.
  const compactFooter = customEntry && active && !pending ? "Enter save · Esc cancel"
    : pending || !active ? footer
    : "↑↓ choose · Enter next/submit\nSpace select · C custom · S skip · Esc cancel";
  const footerLines = wrapToolResult(compactFooter, contentWidth);
  const bodyPageSize = Math.max(1, Math.floor(maxHeight ?? 40) - 4 - footerLines.length);
  const bodyLines: Array<{ text: string; color: string; bold?: boolean }> = [];
  const optionStarts: number[] = [];
  const appendBody = (text: string, color: string, bold = false) => {
    for (const line of wrapToolResult(sanitizeDisplay(text), contentWidth)) {
      bodyLines.push({ text: line, color, bold });
    }
  };
  if (customEntry) {
    appendBody(`Custom answer: ${customText.slice(0, customCursor)}▏${customText.slice(customCursor)}`, theme.text);
    if (state.error) appendBody(state.error, theme.danger);
  } else if (question) {
    appendBody(question.question, theme.text, true);
    options.forEach((option, index) => {
      optionStarts.push(bodyLines.length);
      const selected = draft?.selected.includes(option.label) ?? false;
      const marker = selected ? (question.multi_select ? "☑" : "●") : index === optionIndex ? "›" : " ";
      appendBody(`${marker} ${index + 1}. ${option.label}`, selected ? theme.accentAlt : theme.text);
      if (option.description) appendBody(option.description, theme.muted);
    });
    appendBody(`C. Custom answer${draft?.custom ? ` · ${draft.custom}` : ""}`, theme.accentAlt);
    if (state.error) appendBody(state.error, theme.danger);
  } else {
    appendBody("Question unavailable.", theme.danger);
  }
  const customCursorLine = wrapToolResult(
    sanitizeDisplay(`Custom answer: ${customText.slice(0, customCursor)}▏`), contentWidth,
  ).length - 1;
  const maxScrollOffset = Math.max(0, bodyLines.length - bodyPageSize);
  const safeScrollOffset = Math.min(scrollOffset, maxScrollOffset);
  const focusOption = (index: number) => {
    setOptionIndex(index);
    if (maxHeight !== undefined) setScrollOffset(optionStarts[index] ?? 0);
  };
  useEffect(() => { setScrollOffset(0); }, [request.request_id, state.index]);
  useEffect(() => {
    if (customEntry) setScrollOffset(Math.max(0, customCursorLine - bodyPageSize + 1));
  }, [customEntry, customText, customCursor, contentWidth, bodyPageSize]);
  useEffect(() => {
    if (state.error) setScrollOffset(maxScrollOffset);
  }, [state.error]);

  return (
    <Box
      width={panelWidth}
      borderStyle="round"
      borderColor={active && pending === null ? theme.accentAlt : theme.muted}
      paddingX={1}
      flexDirection="column"
    >
      <Text bold color={theme.accentAlt} wrap="truncate-end">
        {truncateDisplay(
          `◆ Question${request.state === "pending" ? " · work continues · Tab to switch" : ""} · ${state.index + 1}/${state.request.questions.length}${question?.header ? ` · ${question.header}` : ""}`,
          contentWidth,
        )}
      </Text>
      {maxHeight !== undefined ? (
        <>
          {bodyLines.slice(safeScrollOffset, safeScrollOffset + bodyPageSize).map((line, index) => (
            <Text key={index} color={line.color} bold={line.bold}>{line.text}</Text>
          ))}
          <Text color={theme.muted} wrap="truncate-end">
            {`PgUp/PgDn ${safeScrollOffset + 1}-${Math.min(bodyLines.length, safeScrollOffset + bodyPageSize)}/${bodyLines.length}`}
          </Text>
        </>
      ) : question ? (
        <>
          <Text bold color={theme.text} wrap="wrap">
            {sanitizeDisplay(question.question)}
          </Text>
          {options.map((option, index) => {
            const selected = draft?.selected.includes(option.label) ?? false;
            const marker = selected ? (question.multi_select ? "☑" : "●") : index === optionIndex ? "›" : " ";
            return (
              <Box key={`${question.id}-${index}`} flexDirection="column">
                <Text color={selected ? theme.accentAlt : theme.text} wrap="wrap">
                  {sanitizeDisplay(`${marker} ${index + 1}. ${option.label}`)}
                </Text>
                {option.description && (
                  <Box paddingLeft={4}>
                    <Text color={theme.muted} wrap="wrap">
                      {sanitizeDisplay(option.description)}
                    </Text>
                  </Box>
                )}
              </Box>
            );
          })}
          {customEntry ? (
            <Box>
              <Text color={theme.accentAlt}>Custom answer: </Text>
              <TextInput
                value={customText}
                onChange={(next) => setCustomText(singleLineFieldText(next, " "))}
                onSubmit={commitCustom}
                placeholder="Type your answer"
                focus={active && pending === null}
              />
            </Box>
          ) : (
            <Text color={draft?.custom ? theme.accentAlt : theme.text} wrap="truncate-end">
              {truncateDisplay(
                `  C. Custom answer${draft?.custom ? ` · ${draft.custom}` : ""}`,
                contentWidth,
              )}
            </Text>
          )}
          {state.error && (
            <Text color={theme.danger} wrap="truncate-end">
              {truncateDisplay(state.error, contentWidth)}
            </Text>
          )}
        </>
      ) : (
        <Text color={theme.danger}>Question unavailable.</Text>
      )}
      <Text color={active && pending === null ? theme.accentAlt : theme.muted}>{maxHeight !== undefined ? compactFooter : footer}</Text>
    </Box>
  );
}
