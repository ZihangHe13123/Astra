import { randomUUID } from "node:crypto";
import React, { useRef, useState } from "react";
import { Box, Text, useInput } from "ink";
import type { ConnectionRoute, TuiCommand } from "../types.js";
import { singleLineFieldText } from "../paste-command.js";
import { graphemeBoundary } from "../grapheme-editing.js";
import { useTheme } from "../theme-context.js";
import { approvalPaintGlyph } from "../approval-paint.js";

export interface ConnectionPanelProps {
  active?: boolean;
  ownsInput?: () => boolean;
  paintEpoch?: string;
  routes: ConnectionRoute[];
  pending: boolean;
  error: string;
  authorization?: { verification_uri: string; user_code: string } | null;
  onSave: (request: Extract<TuiCommand, { type: "connect_provider" }>) => void;
  onCancel: () => void;
}

function ConnectionField({ initial, hidden, placeholder, submit, active, ownsInput }: {
  initial: string; hidden: boolean; placeholder?: string; submit: (value: string) => void;
  active: boolean; ownsInput: () => boolean;
}) {
  const [display, render] = useState({value: initial, cursor: initial.length});
  const editing = useRef(display);
  useInput((input, key) => {
    if (!active || !ownsInput()) return;
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
    editing.current = next;
    render(next);
  }, { isActive: active });
  const shown = hidden ? "*".repeat(display.value.length) : display.value;
  const end = graphemeBoundary(shown, display.cursor, 1);
  if (!shown && placeholder) return <Text dimColor>{placeholder}</Text>;
  return <Text>{shown.slice(0, display.cursor)}<Text inverse>{shown.slice(display.cursor, end) || " "}</Text>{shown.slice(end)}</Text>;
}

export function ConnectionPanel({ routes, pending, error, authorization, active = true, ownsInput = () => true, paintEpoch, onSave, onCancel }: ConnectionPanelProps) {
  const theme = useTheme();
  const [stage, setStage] = useState<"provider" | "route" | "url" | "auth" | "key" | "env">("provider");
  const [provider, setProvider] = useState("");
  const [route, setRoute] = useState<ConnectionRoute>();
  const [baseUrl, setBaseUrl] = useState("");
  const [fieldKey, resetField] = useState(0);
  const [query, setQuery] = useState("");
  const [index, setIndex] = useState(0);
  const providers = [...new Set(routes.map(r => r.provider))];
  const choices = stage === "provider" ? providers
    : stage === "route" ? routes.filter(r => r.provider === provider).map(r => r.label)
    : stage === "auth" ? ["Paste API key (hidden)", `Use environment variable${route?.key_available ? " (available)" : ""}`,
      ...(baseUrl.match(/^http:\/\/(localhost|127\.0\.0\.1|\[::1\])[:/]/) ? ["Local server without a key"] : [])] : [];
  const filtered = choices.filter(c => c.toLowerCase().includes(query.toLowerCase()));
  const selection = Math.min(index, Math.max(0, filtered.length - 1));
  const textEntry = stage === "url" || stage === "key" || stage === "env";
  const moveTo = (next: typeof stage) => { setStage(next); setIndex(0); setQuery(""); };
  const save = (key: string, env: string) => {
    if (!route) return;
    onSave({ type: "connect_provider", request_id: randomUUID(), route_id: route.id,
      base_url: baseUrl, api_key: key, api_key_env: env });
    resetField(previous => previous + 1);
  };
  const choose = () => {
    const chosen = filtered[selection];
    if (!chosen) return;
    if (stage === "provider") { setProvider(chosen); moveTo("route"); }
    else if (stage === "route") {
      const next = routes.find(r => r.provider === provider && r.label === chosen)!;
      if (next.auth_mode === "oauth") {
        onSave({ type: "connect_provider", request_id: randomUUID(), route_id: next.id,
          base_url: next.base_url, api_key: "", api_key_env: "" });
        return;
      }
      setRoute(next); setBaseUrl(next.base_url); moveTo(next.base_url ? "auth" : "url");
    } else if (stage === "auth") {
      if (chosen.startsWith("Paste")) moveTo("key");
      else if (chosen.startsWith("Use")) moveTo("env");
      else save("", "");
    }
  };
  useInput((input, key) => {
    if (!active || !ownsInput()) return;
    if (key.escape || (key.ctrl && input === "c")) { onCancel(); return; }
    if (pending || textEntry || key.ctrl || key.meta) return;
    if (key.upArrow) setIndex(i => Math.max(0, i - 1));
    else if (key.downArrow) setIndex(i => Math.min(filtered.length - 1, i + 1));
    else if (key.return) choose();
    else if (key.backspace || key.delete) { setQuery(q => q.slice(0, -1)); setIndex(0); }
    else if (input) { setQuery(q => singleLineFieldText(q + input)); setIndex(0); }
  }, { isActive: active });
  const submitText = (value: string) => {
    if (!active || !ownsInput()) return;
    if (stage === "url") { setBaseUrl(value.trim()); moveTo("auth"); }
    else if (stage === "key") save(value.trim(), "");
    else if (stage === "env") save("", value.trim());
  };
  const title = stage === "provider" ? "Choose provider" : stage === "route" ? `${provider} · Choose API route`
    : stage === "url" ? "API base URL for your workspace / region" : stage === "auth" ? "Choose authentication"
    : stage === "key" ? "API key (saved privately on this installation)" : "Environment variable name";
  return <Box flexDirection="column" borderStyle="round" borderColor={theme.border} paddingX={1}>
    <Text bold>{paintEpoch ? approvalPaintGlyph(paintEpoch, false, "h") + " " : ""}{pending ? "Connecting…" : title}</Text>
    {baseUrl && stage !== "provider" && stage !== "route" && <Text color={theme.muted}>{baseUrl}</Text>}
    {error && <Text color="red">{error}</Text>}
    {pending ? authorization ? <Box flexDirection="column">
        <Text>Open {authorization.verification_uri}</Text>
        <Text bold>Enter code: {authorization.user_code}</Text>
        <Text>Waiting for ChatGPT authorization…</Text>
        <Text color={theme.muted}>If disabled, enable Codex device-code login in ChatGPT Settings → Security, then reconnect.</Text>
      </Box> : <Text>Connecting and checking model list…</Text>
      : textEntry ? <ConnectionField key={stage + fieldKey} initial={stage === "env" ? route?.api_key_env ?? "" : ""}
        active={active} ownsInput={ownsInput} submit={submitText} hidden={stage === "key"} placeholder={stage === "url" ? "https://…/v1" : undefined} />
      : <>
        {query && <Text color={theme.muted}>Filter: {query}</Text>}
        {filtered.slice(Math.max(0, selection - 4), Math.max(0, selection - 4) + 7).map(label =>
          <Text key={label} inverse={label === filtered[selection]}>{label === filtered[selection] ? "› " : "  "}{label}</Text>)}
      </>}
    <Text color={theme.muted}>↑/↓ choose · Enter continue · Esc {pending ? "cancel connectio" : "clos"}{approvalPaintGlyph(paintEpoch, false, "t", pending ? "n" : "e")}</Text>
  </Box>;
}
