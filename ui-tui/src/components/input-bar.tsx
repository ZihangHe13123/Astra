import type { LocalModeDefinition } from "../types.js";
import React, { useEffect, useRef, useState, forwardRef, useImperativeHandle } from "react";
import { Text, Box, useInput } from "ink";
import stringWidth from "string-width";
import { graphemeBoundary } from "../grapheme-editing.js";
import { inputViewport } from "../input-viewport.js";
import { useLiveRows } from "./streaming-control-layout.js";
import { useTheme } from "../theme-context.js";
import { randomUUID } from "node:crypto";
import { emptyDraft, appendAppshot, reconcileAppshotInput, mapAppshotOrdinaryText, projectAppshotText, appshotSubmission, freezeAppshotSubmission, settleAppshotSubmission, revokeAppshots, appshotCount, validateAppshotOffer, type AppshotInputState, type AppshotInputOffer, type AppshotManifestReader } from "../appshot-input.js";
import type { AppshotConsumer, AppshotDraftState } from "../appshot-client.js";
import type { ProviderInfo } from "../types.js";
import type { InputSubmission } from "../types.js";
import type { RuntimeMode } from "../types.js";
import type { ThemeName } from "../theme.js";
import { responsiveMode } from "./app-header.js";
import {
  completeSlashCommand,
  type ModelMenuItem,
  normalizeSelectedCommandIndex,
  resolveSlashCommandSubmission,
  type SessionMenuItem,
  slashCommandSuggestions,
  visibleGroupedSuggestionWindow,
} from "../command-menu.js";
import {
  insertImageInputValue,
  normalizeImageInputValue,
  normalizeMultilineInputValue,
  resolveImageInputSubmitText,
  shouldNormalizeImageInputValue,
  updateImageInputValue,
  type ImageInputAttachment,
} from "../image-command.js";
import {
  consumeBracketedPasteChunk,
  resolvePastedTextSubmitText,
  pastedTextPreview,
  updatePastedTextInput,
  type PastedTextAttachment,
} from "../paste-command.js";

export interface AppshotInputHandle extends AppshotConsumer {
  openModelMenu(providerId: string): void;
  snapshot(): AppshotInputState;
  capacity(): AppshotDraftState;
  acceptSubmission(id: string): void;
  rejectSubmission(id: string): void;
  unknownSubmission(id: string): void;
  discardSubmission(id: string): void;
}
interface Props {
  onSubmit: (submission: InputSubmission) => void;
  appshotManifestReader?: AppshotManifestReader;
  onAppshotRelease?: (requestId:string)=>void;
  onAppshotStateChange?: (state:AppshotDraftState)=>void;
  disabled: boolean;
  yolo?: boolean;
  /** Shown while replies are read aloud; brighter while one is being spoken. */
  voice?: "on" | "speaking";
  voices?: string[];
  sessionList: SessionMenuItem[];
  barSessionList?: SessionMenuItem[];
  minimalSessionList?: SessionMenuItem[];
  localSessionList?: SessionMenuItem[];
  localMode?: LocalModeDefinition | null;
  modelList: ModelMenuItem[];
  personas?: { name: string; description: string }[];
  menuRows?: number;
  providers?: ProviderInfo[];
  recentModels?: string[];
  onModelMenuOpen?: (providerId?: string) => void;
  onMenuVisibilityChange?: (visible: boolean) => void;
  menuFocusActive?: boolean;
  currentTheme?: ThemeName;
  runtimeMode?: RuntimeMode;
  columns?: number;
}

export function shortenToWidth(value: string, maxWidth: number): string {
  const width = Math.max(0, Math.floor(maxWidth));
  if (width === 0) return "";
  if (stringWidth(value) <= width) return value;
  if (width === 1) return "…";

  let result = "";
  for (const character of value) {
    if (stringWidth(result + character) + 1 > width) break;
    result += character;
  }
  return `${result}…`;
}

function CursorText({ value, cursor, placeholder, muted }: {
  value: string;
  cursor: number;
  placeholder: string;
  muted: string;
}) {
  if (!value) {
    return (
      <Text>
        <Text inverse>{placeholder.slice(0, 1) || " "}</Text>
        <Text color={muted}>{placeholder.slice(1)}</Text>
      </Text>
    );
  }
  const offset = Math.max(0, Math.min(cursor, value.length));
  const nextOffset = graphemeBoundary(value, offset, 1);
  return (
    <Text>
      {value.slice(0, offset)}
      <Text inverse>{value.slice(offset, nextOffset) || " "}</Text>
      {value.slice(nextOffset)}
    </Text>
  );
}

export function suggestionCommandWidth(
  items: Array<{ command: string }>,
  columns: number,
  mode: "compact" | "standard" | "wide",
): number {
  const longestCommand = Math.max(0, ...items.map((item) => stringWidth(item.command)));
  const preferred = Math.max(mode === "compact" ? 20 : 26, longestCommand + 4);
  const reserved = mode === "wide" ? 42 : mode === "standard" ? 28 : 6;
  return Math.min(preferred, Math.max(16, columns - reserved));
}

export const InputBar = forwardRef<AppshotInputHandle, Props>(function InputBar({
  onSubmit,
  appshotManifestReader, onAppshotRelease, onAppshotStateChange,
  disabled,
  yolo = false,
  voice,
  voices,
  sessionList,
  barSessionList = [],
  minimalSessionList = [],
  localSessionList = [],
  localMode,
  modelList,
  providers, recentModels,
  personas = [],
  menuRows = 8,
  onModelMenuOpen,
  onMenuVisibilityChange,
  menuFocusActive,
  currentTheme,
  runtimeMode = "work",
  columns = process.stdout.columns || 100,
}: Props, ref) {
  const liveRows = useLiveRows();
  const theme = useTheme();
  const chrome = theme.chrome;
  const isBar = runtimeMode === "bar";
  const isMinimal = runtimeMode === "minimal";
  const isLocal = runtimeMode === "local";
  const mode = responsiveMode(columns);
  const [input, setInput] = useState("");
  const [attachments, setAttachments] = useState<ImageInputAttachment[]>([]);
  const [pastedText, setPastedText] = useState<PastedTextAttachment[]>([]);
  const [cursorOffset, setCursorOffset] = useState(0);
  const [selectedCommandIndex, setSelectedCommandIndex] = useState(0);
  const [selectedModelSuggestion, setSelectedModelSuggestion] = useState<string | null>(null);
  const [menuDismissed, setMenuDismissed] = useState(false);
  const [submissionHint, setSubmissionHint] = useState("");
  const submittedValueRef = useRef<string | null>(null);
  const modelMenuOpenRef = useRef<string | null>(null);
  const inputRef = useRef("");
  const attachmentsRef = useRef<ImageInputAttachment[]>([]);
  const pastedTextRef = useRef<PastedTextAttachment[]>([]);
  const cursorOffsetRef = useRef(0);
  const bracketedPasteBufferRef = useRef<string | null>(null);
  const reportedMenuVisibilityRef = useRef(false);
  const appshotRef = useRef<AppshotInputState>({ draft: emptyDraft() });
  const stagedRef = useRef(new Map<string, AppshotInputOffer>());
  const pendingAuxRef = useRef<{
    images: ImageInputAttachment[];
    pastes: PastedTextAttachment[];
  }>();
  const [pendingHint, setPendingHint] = useState("");
  const capacity = (): AppshotDraftState => ({
    appshotCount: appshotCount(appshotRef.current),
    canAccept:
      !disabled && appshotCount(appshotRef.current) + stagedRef.current.size < 4,
  });
  const publishAppshots = () => {
    const state = appshotRef.current;
    inputRef.current = state.draft.text;
    cursorOffsetRef.current = Math.min(
      cursorOffsetRef.current,
      state.draft.text.length,
    );
    setInput(state.draft.text);
    setCursorOffset(cursorOffsetRef.current);
    setPendingHint(
      state.pending
        ? `Appshot submission ${state.pending.status}. /appshot pending status · /appshot pending discard`
        : "",
    );
    onAppshotStateChange?.(capacity());
  };
  const releaseRemoved = (
    before: AppshotInputState,
    after: AppshotInputState,
  ) => {
    for (const a of before.draft.attachments)
      if (!after.draft.attachments.some((b) => a.requestId === b.requestId))
        onAppshotRelease?.(a.requestId);
  };
  const settleSubmission = (
    id: string,
    result: "accepted" | "rejected" | "unknown",
  ) => {
    const state = appshotRef.current;
    if (state.pending?.submissionId !== id) return;
    if (result === "rejected" && pendingAuxRef.current) {
      // Rename newer independent placeholders before concatenation, preserving each payload.
      const old = pendingAuxRef.current;
      let draft = state.draft;
      const nextNumber = (items: Array<{label:string}>) =>
        Math.max(0, ...items.map(item => Number(item.label.match(/#(\d+)/)?.[1] ?? 0))) + 1;
      let imageNumber = nextNumber([...old.images, ...attachmentsRef.current]);
      let pasteNumber = nextNumber([...old.pastes, ...pastedTextRef.current]);
      const rename = <T extends { label: string }>(
        items: T[],
        kind: "Image" | "Pasted text",
      ): T[] =>
        items.map((a) => {
          const label =
            kind === "Image"
              ? `[Image #${imageNumber++}]`
              : a.label.replace(/#\d+/, `#${pasteNumber++}`);
          draft = mapAppshotOrdinaryText(draft, text => text.split(a.label).join(label));
          return { ...a, label };
        });
      attachmentsRef.current = [
        ...old.images,
        ...rename(attachmentsRef.current, "Image"),
      ];
      pastedTextRef.current = [
        ...old.pastes,
        ...rename(pastedTextRef.current, "Pasted text"),
      ];
      appshotRef.current = { ...state, draft };
      setAttachments(attachmentsRef.current);
      setPastedText(pastedTextRef.current);
    }
    if (result === "accepted")
      for (const a of state.pending.draft.attachments)
        onAppshotRelease?.(a.requestId);
    appshotRef.current = settleAppshotSubmission(appshotRef.current, id, result);
    if (result !== "unknown") pendingAuxRef.current = undefined;
    publishAppshots();
  };
  const canStage = (requestId: string) => capacity().canAccept
    && !stagedRef.current.has(requestId)
    && !appshotRef.current.draft.attachments.some(a => a.requestId === requestId)
    && !appshotRef.current.pending?.draft.attachments.some(a => a.requestId === requestId);
  useImperativeHandle(ref, () => ({
    openModelMenu: (providerId: string) => {
      const value = `/model ${providerId}::`;
      commitInput(value, value.length);
    },
    snapshot: () => appshotRef.current,
    capacity,
    stage: (offer, binding) => {
      if (!canStage(offer.request_id)) return false;
      try {
        stagedRef.current.set(
          offer.request_id,
          validateAppshotOffer(offer, binding, appshotManifestReader),
        );
        return true;
      } catch {
        return false;
      }
    },
    stageWindows: (offer) => {
      // Only AppshotClient's verified native-read path calls this entry point.
      if (!("recipient" in offer.binding) || !canStage(offer.requestId)) return false;
      stagedRef.current.set(offer.requestId, offer);
      return true;
    },
    commit: (event) => {
      const offer = stagedRef.current.get(event.request_id);
      if (
        !offer ||
        offer.manifestPath !== event.manifest_path ||
        offer.binding.instance_id !== event.broker_id ||
        offer.binding.session_id !== event.session_id ||
        appshotCount(appshotRef.current) >= 4
      )
        throw new Error("attachment_rejected");
      const before = appshotRef.current;
      const draft = appendAppshot(before.draft, offer, cursorOffsetRef.current);
      appshotRef.current = { ...before, draft };
      stagedRef.current.delete(event.request_id);
      releaseRemoved(before, appshotRef.current);
      cursorOffsetRef.current =
        draft.attachments.find((a) => a.requestId === event.request_id)!.start +
        draft.attachments.find((a) => a.requestId === event.request_id)!.label
          .length;
      // Commit receipt is emitted by AppshotClient after this synchronous state incorporation.
      publishAppshots();
      return capacity();
    },
    revoke: (event) => {
      stagedRef.current.delete(event.requestID);
      appshotRef.current = revokeAppshots(
        appshotRef.current,
        new Set([event.requestID]),
      );
      publishAppshots();
    },
    disconnect: () => {
      stagedRef.current.clear();
      appshotRef.current = revokeAppshots(appshotRef.current);
      publishAppshots();
      return capacity();
    },
    acceptSubmission: (id) => settleSubmission(id, "accepted"),
    rejectSubmission: (id) => settleSubmission(id, "rejected"),
    unknownSubmission: (id) => settleSubmission(id, "unknown"),
    discardSubmission: (id) => settleSubmission(id, "accepted"),
  }));
  useEffect(() => {
    onAppshotStateChange?.(capacity());
  }, [disabled, onAppshotStateChange]);
  const commandContext = {
    providers, recentModels,
    themeName: currentTheme,
    barSessions: barSessionList,
    minimalSessions: minimalSessionList,
    localSessions: localSessionList,
    localMode,
    personas,
    voices,
  };
  const commandSuggestions = disabled || menuDismissed ? [] : slashCommandSuggestions(input, sessionList, modelList, commandContext);
  const modelSuggestionIndex = selectedModelSuggestion === null ? -1 : commandSuggestions.findIndex(
    s => (s.submitValue ?? s.completion ?? s.command) === selectedModelSuggestion,
  );
  const normalizedSelectedCommandIndex = normalizeSelectedCommandIndex(
    modelSuggestionIndex >= 0 ? modelSuggestionIndex : selectedCommandIndex,
    commandSuggestions.length,
  );
  const menuVisible = commandSuggestions.length > 0;
  const renderMenu = menuVisible && (menuFocusActive ?? true);
  const visibleSuggestions = visibleGroupedSuggestionWindow(
    commandSuggestions,
    normalizedSelectedCommandIndex,
    Math.max(1, Math.min(menuRows, liveRows - 10)),
  );
  const commandWidth = suggestionCommandWidth(visibleSuggestions, columns, mode);

  const reportMenuVisibility = (visible: boolean) => {
    if (reportedMenuVisibilityRef.current === visible) return;
    reportedMenuVisibilityRef.current = visible;
    onMenuVisibilityChange?.(visible);
  };

  const menuVisibilityForInput = (value: string) => (
    !disabled && slashCommandSuggestions(value, sessionList, modelList, commandContext).length > 0
  );

  useEffect(() => {
    reportMenuVisibility(menuVisible);
  }, [menuVisible, onMenuVisibilityChange]);

  useEffect(() => {
    const isOpen = /^\/model(?:\s|$)/i.test(input.trimStart());
    const providerId = input.trimStart().match(/^\/model\s+([^\s]+?)::/)?.[1] ?? "";
    if (isOpen && modelMenuOpenRef.current !== providerId) onModelMenuOpen?.(providerId || undefined);
    modelMenuOpenRef.current = isOpen ? providerId : null;
  }, [input, onModelMenuOpen]);

  const commitInput = (nextValue: string, nextCursor: number) => {
    const before=appshotRef.current;
    const reconciled=reconcileAppshotInput(before.draft,nextValue);
    appshotRef.current={...before,draft:reconciled};
    releaseRemoved(before,appshotRef.current);
    // Use the same identity markers on both sides of paste reconciliation, then
    // restore by marker positions. Nested captured labels never enter either parser.
    const markers = new Map([...before.draft.attachments, ...reconciled.attachments].map(
      a => [a.requestId, `ASTRA_APPSHOT_${randomUUID().replace(/-/g, '')}`],
    ));
    const shield = (draft: typeof reconciled) => projectAppshotText(draft, a => markers.get(a.requestId)!);
    const pasteUpdate = updatePastedTextInput(shield(before.draft), shield(reconciled), pastedTextRef.current);
    let value = pasteUpdate.displayText;
    pastedTextRef.current = pasteUpdate.attachments;
    setPastedText(pasteUpdate.attachments);
    setSelectedCommandIndex(0);
    setSelectedModelSuggestion(null);
    setMenuDismissed(false);
    submittedValueRef.current = null;
    setSubmissionHint("");
    let nextAttachments = attachmentsRef.current;
    if (nextAttachments.length > 0) {
      if (shouldNormalizeImageInputValue(value, nextAttachments)) {
        const normalized = updateImageInputValue(value, nextAttachments);
        value = normalized.displayText;
        nextAttachments = normalized.attachments;
      } else {
        nextAttachments = nextAttachments.filter((attachment) => value.includes(attachment.label));
      }
    } else {
      const normalized = normalizeImageInputValue(value);
      if (normalized) {
        value = normalized.displayText;
        nextAttachments = normalized.attachments;
      }
    }

    const restored = reconciled.attachments.map(a => ({
      ...a, start: value.indexOf(markers.get(a.requestId)!),
    })).filter(a => a.start >= 0);
    for (const a of [...restored].sort((a, b) => a.start - b.start)) {
      const marker = markers.get(a.requestId)!;
      const at = a.start;
      value = value.slice(0, at) + a.label + value.slice(at + marker.length);
      for (const other of restored)
        if (other.start > at) other.start += a.label.length - marker.length;
    }
    const normalizedState = { ...appshotRef.current, draft: { ...reconciled, text: value, attachments: restored } };
    releaseRemoved(appshotRef.current,normalizedState);
    appshotRef.current=normalizedState;
    onAppshotStateChange?.(capacity());
    const transformed = value !== nextValue;
    const cursor = transformed ? value.length : Math.max(0, Math.min(nextCursor, value.length));
    reportMenuVisibility(menuVisibilityForInput(value));
    inputRef.current = value;
    attachmentsRef.current = nextAttachments;
    cursorOffsetRef.current = cursor;
    setInput(value);
    setAttachments(nextAttachments);
    setCursorOffset(cursor);
  };

  const insertText = (text: string) => {
    if (!text) return;
    const inserted = insertImageInputValue(inputRef.current, text, cursorOffsetRef.current);
    commitInput(inserted.value, inserted.cursor);
  };

  const applyCommandCompletion = () => {
    const completed = completeSlashCommand(inputRef.current, normalizedSelectedCommandIndex, sessionList, modelList, commandContext);
    if (!completed) return false;
    commitInput(completed, completed.length);
    return true;
  };

  const submitInput = (value: string) => {
    const leading = value.length - value.trimStart().length;
    const localText = value.slice(leading);
    const commandBoundary = (end: number) =>
      end === value.length || /\s/.test(value[end]) ||
      appshotRef.current.draft.attachments.some(a => a.start === end);
    const localPrefix = localText.match(/^\/(?:appshot|reconnect)/i)?.[0];
    if (localPrefix && commandBoundary(leading + localPrefix.length)) {
      // Consume only the local command prefix, leaving newer typed captures/text editable.
      const candidate = localText.match(/^(?:\/appshot(?:\s+pending\s+(?:status|discard)|\s+(?:status|enable|disable))|\/reconnect)/i)?.[0];
      const known = candidate && commandBoundary(leading + candidate.length) ? candidate : undefined;
      const firstAttachment = Math.min(value.length, ...appshotRef.current.draft.attachments.map(a => a.start));
      const lineEnd = value.indexOf("\n", leading);
      const command = known ?? value.slice(leading, Math.min(firstAttachment, lineEnd < 0 ? value.length : lineEnd)).trimEnd();
      const consumedEnd = leading + command.length;
      commitInput(value.slice(consumedEnd), 0);
      onSubmit({text: command, appshots: []});
      return;
    }
    if (appshotRef.current.pending) {
      setSubmissionHint("Resolve the pending Appshot submission first.");
      return;
    }
    const draft=appshotRef.current.draft;
    const submission=draft.attachments.length ? appshotSubmission(draft,randomUUID()) : {text:value,appshots:[]};
    const trimmed = normalizeMultilineInputValue(submission.text).trim();
    if (!trimmed || submittedValueRef.current === trimmed) return;
    const withImages = resolveImageInputSubmitText(trimmed, attachmentsRef.current);
    const expanded = resolvePastedTextSubmitText(withImages, pastedTextRef.current);
    const decision = resolveSlashCommandSubmission(
      expanded,
      normalizedSelectedCommandIndex,
      sessionList,
      modelList,
      commandContext,
    );
    if (decision.kind === "blocked") {
      submittedValueRef.current = null;
      setSubmissionHint(decision.message.slice(0, 160));
      return;
    }
    const text = decision.kind === "submit" ? decision.value : expanded;
    if (!text) return;
    reportMenuVisibility(false);
    submittedValueRef.current = trimmed;
    setSubmissionHint("");
    if(draft.attachments.length){
      pendingAuxRef.current={images:attachmentsRef.current,pastes:pastedTextRef.current};
      appshotRef.current=freezeAppshotSubmission(appshotRef.current,submission.submissionId!);
    } else appshotRef.current={...appshotRef.current,draft:emptyDraft(draft.nextNumber)};
    inputRef.current = "";
    attachmentsRef.current = [];
    pastedTextRef.current = [];
    cursorOffsetRef.current = 0;
    bracketedPasteBufferRef.current = null;
    setInput("");
    setAttachments([]);
    setPastedText([]);
    setCursorOffset(0);
    setSelectedCommandIndex(0);
    setMenuDismissed(false);
    publishAppshots();
    // Freeze and clear refs before invoking parent: synchronous rejection cannot erase restored edits.
    onSubmit({...submission,text});
    setTimeout(() => {
      submittedValueRef.current = null;
    }, 0);
  };

  useInput((rawInput, key) => {
    if (disabled) return;

    const bracketed = consumeBracketedPasteChunk(bracketedPasteBufferRef.current, rawInput);
    if (bracketed.handled) {
      bracketedPasteBufferRef.current = bracketed.buffer;
      if (bracketed.completed !== undefined) insertText(bracketed.completed);
      if (bracketed.trailing) insertText(bracketed.trailing);
      return;
    }

    if (commandSuggestions.length > 0 && (key.upArrow || key.downArrow)) {
      const next = normalizeSelectedCommandIndex(normalizedSelectedCommandIndex + (key.upArrow ? -1 : 1), commandSuggestions.length);
      setSelectedCommandIndex(next);
      if (providers !== undefined && /^\/model(?:\s|$)/.test(inputRef.current)) {
        const item = commandSuggestions[next];
        setSelectedModelSuggestion(item.submitValue ?? item.completion ?? item.command);
      }
      return;
    }
    if (commandSuggestions.length > 0 && key.escape) {
      reportMenuVisibility(false);
      setMenuDismissed(true);
      return;
    }
    if (commandSuggestions.length > 0 && key.tab) {
      applyCommandCompletion();
      return;
    }
    if (key.return) {
      const selected = commandSuggestions[normalizedSelectedCommandIndex];
      if (selected?.kind === "submenu") {
        applyCommandCompletion();
        return;
      }
      submitInput(inputRef.current);
      return;
    }
    if (key.leftArrow || key.rightArrow) {
      const delta = key.leftArrow ? -1 : 1;
      const cursor = graphemeBoundary(inputRef.current, cursorOffsetRef.current, delta);
      cursorOffsetRef.current = cursor;
      setCursorOffset(cursor);
      return;
    }
    if (key.backspace || key.delete) {
      const current = inputRef.current;
      const cursor = cursorOffsetRef.current;
      if (cursor > 0) {
        const previous = graphemeBoundary(current, cursor, -1);
        commitInput(current.slice(0, previous) + current.slice(cursor), previous);
      }
      return;
    }
    if (key.ctrl || key.meta || key.tab || key.upArrow || key.downArrow || key.escape) return;
    insertText(rawInput);
  });

  const prompt = isBar ? "say" : isMinimal || isLocal ? "you" : chrome?.promptLabel ?? "you";
  const viewport = inputViewport(input, cursorOffset, Math.max(1,
    columns - 4 - stringWidth(prompt) - 1 - (yolo && !isBar && !isLocal ? 5 : 0) - (voice ? 6 : 0)));

  return (
    <Box flexDirection="column">
      {renderMenu && (
        <Box paddingX={1} flexDirection="column">
          {visibleSuggestions.map((item, visibleIndex) => {
            const selected = item === commandSuggestions[normalizedSelectedCommandIndex];
            const showGroup = Boolean(item.group && (visibleIndex === 0 || visibleSuggestions[visibleIndex - 1]?.group !== item.group));
            const descriptionBudget = Math.max(16, columns - commandWidth - 7);
            return (
              <React.Fragment key={`${item.group ?? "command"}-${item.command}`}>
                {showGroup && <Text bold color={theme.subtle}>{`  ${item.group}`}</Text>}
                <Text wrap="truncate-end">
                  <Text
                    color={selected ? theme.accent : item.current ? theme.success : theme.muted}
                    backgroundColor={selected ? theme.menuSelectedBackground : theme.menuBackground}
                    inverse={theme.menuInverse && selected}
                  >
                    {(selected ? `${chrome?.menuMarker ?? "›"} ` : "  ") + `${item.current ? "● " : "  "}${shortenToWidth(item.command, commandWidth - 4)}`.padEnd(commandWidth)}
                  </Text>
                  {mode !== "compact" && (
                    <Text
                      dimColor
                      color={theme.muted}
                      backgroundColor={selected ? theme.menuSelectedBackground : theme.menuBackground}
                    >
                      {shortenToWidth(item.description, descriptionBudget)}
                      {selected && visibleSuggestions.length < commandSuggestions.length
                        ? ` · ${normalizedSelectedCommandIndex + 1}/${commandSuggestions.length}`
                        : ""}
                    </Text>
                  )}
                </Text>
              </React.Fragment>
            );
          })}
          <Text dimColor color={theme.subtle} wrap="truncate-end">  ↑↓ select  Enter run  Tab complete  Esc close</Text>
        </Box>
      )}
      <Box borderStyle={chrome?.inputFrameStyle ?? chrome?.frameStyle ?? "single"} borderColor={disabled ? theme.subtle : theme.accentAlt} paddingX={1}>
        {yolo && !isBar && !isLocal && <Text bold color={theme.warning}>YOLO </Text>}
        {voice && <Text bold={voice === "speaking"} dimColor={voice === "on"} color={theme.accentAlt}>VOICE </Text>}
        <Text bold={theme.prefixBold} color={theme.accent}>
          {prompt}{" "}
        </Text>
        {disabled ? (
          <Text color={theme.muted}>waiting for model...</Text>
        ) : (
          <CursorText
            value={viewport.value}
            cursor={viewport.cursor}
            placeholder={isBar ? "talk over the rain..." : isMinimal ? "type a request" : isLocal ? localMode?.ui.placeholder ?? "type a message" : chrome?.promptPlaceholder ?? "type a message"}
            muted={theme.muted}
          />
        )}
      </Box>
      {pendingHint && <Box paddingX={1}><Text wrap="truncate-end" color={theme.warning}>{pendingHint}</Text></Box>}
      {submissionHint && (
        <Box paddingX={1}>
          <Text wrap="truncate-end" color={theme.warning}>{submissionHint}</Text>
        </Box>
      )}
      {pastedText.length > 0 && (
        <Box paddingX={1} flexDirection="column">
          {pastedText.slice(-1).map((attachment) => (
            <Text wrap="truncate-end" key={attachment.label} color={theme.muted}>
              <Text color={theme.accentAlt}>PASTE </Text>
              {attachment.label}  {pastedTextPreview(attachment.content)}
            </Text>
          ))}
          <Text dimColor color={theme.subtle} wrap="truncate-end">  {pastedText.length} paste(s) preserved · full text sent on Enter</Text>
        </Box>
      )}
    </Box>
  );
});
