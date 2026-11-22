import { randomUUID } from "node:crypto";
import React, { useLayoutEffect, useMemo, useRef, useState } from "react";
import { Box, Text, useInput, useStdout } from "ink";
import type { ConnectionRoute, TuiCommand } from "../types.js";
import { singleLineFieldText } from "../paste-command.js";
import { graphemeBoundary } from "../grapheme-editing.js";
import { useTheme } from "../theme-context.js";
import { escapeApprovalDisplayText } from "@astra/ui-core/approval-display";
import { approvalDraftRow, approvalPaintGlyph } from "../approval-paint.js";
import { draftFingerprint, MAX_DRAFT_ROWS } from "../approval-draft.js";
import { wrapToolResult } from "../tool-results.js";

export interface ConnectionPanelProps {
  width?: number;
  active?: boolean;
  ownsInput: (requirePaint?: boolean, selectedRowDigest?: string) => boolean;
  paintTarget?: object;
  onPaintTarget: (target: object) => void;
  paintEpoch?: string;
  routes: ConnectionRoute[];
  pending: boolean;
  error: string;
  authorization?: { verification_uri: string; user_code: string } | null;
  onSave: (request: Extract<TuiCommand, { type: "connect_provider" }>) => void;
  onCancel: () => void;
}

function ConnectionField({ initial, hidden, placeholder, submit, active, canEdit, changed }: {
  initial: string; hidden: boolean; placeholder?: string; submit: (value: string) => void;
  active: boolean; canEdit: () => boolean; changed: () => void;
}) {
  const [display, render] = useState({value: initial, cursor: initial.length});
  const editing = useRef(display);
  useInput((input, key) => {
    if (!active || !canEdit()) return;
    const {value, cursor} = editing.current;
    if (key.return) { submit(value); return; }
    if (key.escape || key.ctrl || key.tab || key.upArrow || key.downArrow) return;
    let next = editing.current;
    if (key.leftArrow || key.rightArrow) next = {value, cursor: graphemeBoundary(value, cursor, key.leftArrow ? -1 : 1)};
    else if (key.backspace || key.delete) {
      const before = graphemeBoundary(value, cursor, -1);
      next = {value: value.slice(0, before) + value.slice(cursor), cursor: before};
    } else if (input) {
      const prefix = singleLineFieldText(value.slice(0, cursor) + input);
      next = {value: prefix + value.slice(cursor), cursor: prefix.length};
    }
    if (next.value !== value) changed();
    editing.current = next;
    render(next);
  }, { isActive: active });
  const shown = hidden ? "*".repeat(display.value.length) : display.value;
  const end = graphemeBoundary(shown, display.cursor, 1);
  if (!shown && placeholder) return <Text dimColor>{placeholder}</Text>;
  return <Text>{shown.slice(0, display.cursor)}<Text inverse>{shown.slice(display.cursor, end) || " "}</Text>{shown.slice(end)}</Text>;
}

type Option = { key: string; label: string; route?: ConnectionRoute };
const option = (label: string): Option => ({ key: label, label });

// Public route metadata only. Preserve the distinction between a literal escape and a control,
// and between a bracket in a label and the actual delimiters of the route ID.
const routeText = (s: string) => escapeApprovalDisplayText(s.replaceAll("\\", "\\\\"))
  .replaceAll("[", "\\[").replaceAll("]", "\\]").replaceAll("\u2028", "\\u{2028}").replaceAll("\u2029", "\\u{2029}");

function sameDestination(selected: ConnectionRoute, current?: ConnectionRoute) {
  return current?.id === selected.id && current.provider === selected.provider && current.label === selected.label &&
    current.auth_mode === selected.auth_mode && current.base_url === selected.base_url && current.api_key_env === selected.api_key_env;
}

export function ConnectionPanel({ routes, pending, error, authorization, width, active = true, ownsInput, paintTarget, onPaintTarget, paintEpoch, onSave, onCancel }: ConnectionPanelProps) {
  const theme = useTheme();
  const [stage, setStage] = useState<"provider" | "route" | "url" | "auth" | "key" | "env">("provider");
  const [provider, setProvider] = useState("");
  const [route, setRoute] = useState<ConnectionRoute>();
  const [baseUrl, setBaseUrl] = useState("");
  const [fieldKey, resetField] = useState(0);
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const providers = [...new Set(routes.map(r => r.provider))];
  const hasProvider = providers.includes(provider);
  const obsolete = !pending && !!route && !sameDestination(route, routes.find(r => r.id === route.id));
  useLayoutEffect(() => {
    if (!obsolete) return;
    // A key or URL drafted for a removed/reconfigured route must never follow its replacement.
    setRoute(undefined); setBaseUrl(""); setIndex(0); setQuery(""); setStage(hasProvider ? "route" : "provider");
  }, [obsolete, hasProvider]);
  const choices: Option[] = stage === "provider" ? providers.map(option)
    : stage === "route" ? routes.filter(r => r.provider === provider).map(r => ({ key: r.id, label: `${routeText(r.label)} [${routeText(r.id)}]`, route: r }))
    : stage === "auth" ? ["Paste API key (hidden)", `Use environment variable${route?.key_available ? " (available)" : ""}`,
      ...(baseUrl.match(/^http:\/\/(localhost|127\.0\.0\.1|\[::1\])[:/]/) ? ["Local server without a key"] : [])].map(option) : [];
  const filtered = choices.filter(c => c.label.toLowerCase().includes(query.toLowerCase()));
  const selection = Math.min(index, Math.max(0, filtered.length - 1));
  const chosen = filtered[selection];
  const selectedRow = stage === "route" && chosen ? `› ${chosen.label}` : undefined;
  const { stdout } = useStdout();
  // Two borders, two padding cells and two proof rails. Wrap before Ink lays out the cells so
  // each physical row has its own index and the verifier rejoins exactly the original option.
  const rowWidth = (width ?? stdout.columns - 1) - 6;
  const selectedRows = useMemo(() => {
    if (!selectedRow || selectedRow.length > 8192 || !Number.isSafeInteger(rowWidth) || rowWidth < 1) return;
    const parts = wrapToolResult(selectedRow, rowWidth);
    return parts.length <= MAX_DRAFT_ROWS ? parts : undefined;
  }, [selectedRow, rowWidth]);
  const selectedDigest = useMemo(() => selectedRows && selectedRow ? draftFingerprint([selectedRow]) : undefined, [selectedRows, selectedRow]);
  const textEntry = stage === "url" || stage === "key" || stage === "env";
  // Only opaque identity leaves the form. A draft edit changes its revision, never a marker's text.
  const [revision, revise] = useState(0);
  const page = useMemo(() => ({}), [active, pending, stage, provider, route, fieldKey]);
  const destination = chosen?.route ?? route;
  const target = useMemo(() => ({}), [page, chosen?.key, chosen?.label, destination?.id, destination?.provider,
    destination?.auth_mode, destination?.base_url, destination?.api_key_env, destination?.key_available, baseUrl, revision]);
  const live = useRef<object>(), editingPage = useRef<object>();
  useLayoutEffect(() => {
    live.current = target; onPaintTarget(target);
    return () => { live.current = undefined; };
  }, [target, onPaintTarget]);
  useLayoutEffect(() => { editingPage.current = undefined; }, [page]);
  const marked = active && !obsolete && target === paintTarget ? paintEpoch : undefined;
  const canCommit = (expectedRow?: string) => !!marked && live.current === target && ownsInput(true, expectedRow);
  const canEdit = () => {
    if (!active || obsolete || !ownsInput(false)) return false;
    if (canCommit()) editingPage.current = page;
    return editingPage.current === page;
  };
  // Invalidate synchronously: two input callbacks need not be separated by a React commit.
  const changed = () => { live.current = undefined; revise(n => n + 1); };
  const moveTo = (next: typeof stage) => { changed(); setStage(next); setIndex(0); setQuery(""); };
  const save = (key: string, env: string) => {
    if (!route || !canCommit()) return;
    changed();
    onSave({ type: "connect_provider", request_id: randomUUID(), route_id: route.id,
      base_url: baseUrl, api_key: key, api_key_env: env });
    resetField(previous => previous + 1);
  };
  const choose = () => {
    if (!chosen || (selectedRow && !selectedRows) || !canCommit(selectedDigest)) return;
    if (stage === "provider") { setProvider(chosen.key); moveTo("route"); }
    else if (stage === "route") {
      const next = chosen.route!;
      if (next.auth_mode === "oauth") {
        changed();
        onSave({ type: "connect_provider", request_id: randomUUID(), route_id: next.id,
          base_url: next.base_url, api_key: "", api_key_env: "" });
        return;
      }
      setRoute(next); setBaseUrl(next.base_url); moveTo(next.base_url ? "auth" : "url");
    } else if (stage === "auth") {
      if (chosen.key.startsWith("Paste")) moveTo("key");
      else if (chosen.key.startsWith("Use")) moveTo("env");
      else save("", "");
    }
  };
  useInput((input, key) => {
    if (!canEdit()) return;
    if (key.escape || (key.ctrl && input === "c")) { if (canCommit()) onCancel(); return; }
    if (pending || textEntry || key.ctrl || key.meta) return;
    if (key.return) { choose(); return; }
    if (!key.upArrow && !key.downArrow && !key.backspace && !key.delete && !input) return;
    changed();
    if (key.upArrow) setIndex(i => Math.max(0, i - 1));
    else if (key.downArrow) setIndex(i => Math.min(filtered.length - 1, i + 1));
    else if (key.backspace || key.delete) { setQuery(q => q.slice(0, -1)); setIndex(0); }
    else if (input) { setQuery(q => singleLineFieldText(q + input)); setIndex(0); }
  }, { isActive: active });
  const submitText = (value: string) => {
    if (!canCommit()) return;
    if (stage === "url") { setBaseUrl(value.trim()); moveTo("auth"); }
    else if (stage === "key") save(value.trim(), "");
    else if (stage === "env") save("", value.trim());
  };
  const title = stage === "provider" ? "Choose provider" : stage === "route" ? `${provider} · Choose API route`
    : stage === "url" ? "API base URL for your workspace / region" : stage === "auth" ? "Choose authentication"
    : stage === "key" ? "API key (saved privately on this installation)" : "Environment variable name";
  return <Box flexDirection="column" borderStyle="round" borderColor={theme.border} paddingX={1}>
    <Text bold>{marked ? approvalPaintGlyph(marked, false, "h") + " " : ""}{pending ? "Connecting…" : title}</Text>
    {baseUrl && stage !== "provider" && stage !== "route" && <Text color={theme.muted}>{baseUrl}</Text>}
    {error && <Text color="red">{error}</Text>}
    {pending ? authorization ? <Box flexDirection="column">
        <Text>Open {authorization.verification_uri}</Text>
        <Text bold>Enter code: {authorization.user_code}</Text>
        <Text>Waiting for ChatGPT authorization…</Text>
        <Text color={theme.muted}>If disabled, enable Codex device-code login in ChatGPT Settings → Security, then reconnect.</Text>
      </Box> : <Text>Connecting and checking model list…</Text>
      : textEntry ? <ConnectionField key={stage + fieldKey} initial={stage === "env" ? route?.api_key_env ?? "" : ""}
        active={active} canEdit={canEdit} changed={changed} submit={submitText} hidden={stage === "key"} placeholder={stage === "url" ? "https://…/v1" : undefined} />
      : <>
        {query && <Text color={theme.muted}>Filter: {query}</Text>}
        {filtered.slice(Math.max(0, selection - 4), Math.max(0, selection - 4) + 7).map(item =>
          item === chosen && selectedRow ? <Box key={item.key} flexDirection="column" flexShrink={0}>
            {selectedRows ? selectedRows.map((text, i) => <Text key={i} wrap="truncate-end">
              {approvalDraftRow(marked, i, { text, newLine: i === 0 })}</Text>) : <Text>Selected route cannot be displayed in full.</Text>}
          </Box>
          : <Text key={item.key} inverse={item === chosen}>{item === chosen ? "› " : "  "}{item.label}</Text>)}
      </>}
    <Text color={theme.muted}>↑/↓ choose · Enter continue · Esc {pending ? "cancel connectio" : "clos"}{approvalPaintGlyph(marked, false, "t", pending ? "n" : "e")}</Text>
  </Box>;
}
