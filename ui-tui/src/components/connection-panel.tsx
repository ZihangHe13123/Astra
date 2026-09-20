import { randomUUID } from "node:crypto";
import React, { useState } from "react";
import { Box, Text, useInput } from "ink";
import TextInput from "ink-text-input";
import type { ConnectionRoute, TuiCommand } from "../types.js";
import { useTheme } from "../theme-context.js";

export interface ConnectionPanelProps {
  routes: ConnectionRoute[];
  pending: boolean;
  error: string;
  authorization?: { verification_uri: string; user_code: string } | null;
  onSave: (request: Extract<TuiCommand, { type: "connect_provider" }>) => void;
  onCancel: () => void;
}

export function ConnectionPanel({ routes, pending, error, authorization, onSave, onCancel }: ConnectionPanelProps) {
  const theme = useTheme();
  const [stage, setStage] = useState<"provider" | "route" | "url" | "auth" | "key" | "env">("provider");
  const [provider, setProvider] = useState("");
  const [route, setRoute] = useState<ConnectionRoute>();
  const [baseUrl, setBaseUrl] = useState("");
  const [value, setValue] = useState("");
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
  const moveTo = (next: typeof stage) => { setStage(next); setIndex(0); setQuery(""); setValue(""); };
  const save = (key: string, env: string) => {
    if (!route) return;
    onSave({ type: "connect_provider", request_id: randomUUID(), route_id: route.id,
      base_url: baseUrl, api_key: key, api_key_env: env });
    setValue(""); // Do not retain credentials while showing network progress/errors.
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
      else if (chosen.startsWith("Use")) { moveTo("env"); setValue(route?.api_key_env ?? ""); }
      else save("", "");
    }
  };
  useInput((input, key) => {
    if (key.escape || (key.ctrl && input === "c")) { onCancel(); return; }
    if (pending || textEntry || key.ctrl || key.meta) return;
    if (key.upArrow) setIndex(i => Math.max(0, i - 1));
    else if (key.downArrow) setIndex(i => Math.min(filtered.length - 1, i + 1));
    else if (key.return) choose();
    else if (key.backspace || key.delete) { setQuery(q => q.slice(0, -1)); setIndex(0); }
    else if (input) { setQuery(q => q + input.replace(/[\x00-\x1f\x7f]/g, "")); setIndex(0); }
  });
  const submitText = () => {
    if (stage === "url") { setBaseUrl(value.trim()); moveTo("auth"); }
    else if (stage === "key") save(value.trim(), "");
    else if (stage === "env") save("", value.trim());
  };
  const title = stage === "provider" ? "Choose provider" : stage === "route" ? `${provider} · Choose API route`
    : stage === "url" ? "API base URL for your workspace / region" : stage === "auth" ? "Choose authentication"
    : stage === "key" ? "API key (saved privately on this installation)" : "Environment variable name";
  return <Box flexDirection="column" borderStyle="round" borderColor={theme.border} paddingX={1}>
    <Text bold>{pending ? "Connecting…" : title}</Text>
    {baseUrl && stage !== "provider" && stage !== "route" && <Text dimColor>{baseUrl}</Text>}
    {error && <Text color="red">{error}</Text>}
    {pending ? authorization ? <Box flexDirection="column">
        <Text>Open {authorization.verification_uri}</Text>
        <Text bold>Enter code: {authorization.user_code}</Text>
        <Text>Waiting for ChatGPT authorization…</Text>
        <Text dimColor>If disabled, enable Codex device-code login in ChatGPT Settings → Security, then reconnect.</Text>
      </Box> : <Text>Connecting and checking model list…</Text>
      : textEntry ? <TextInput value={value} onChange={setValue} onSubmit={submitText}
        mask={stage === "key" ? "*" : undefined} placeholder={stage === "url" ? "https://…/v1" : undefined} />
      : <>
        {query && <Text dimColor>Filter: {query}</Text>}
        {filtered.slice(Math.max(0, selection - 4), Math.max(0, selection - 4) + 7).map(label =>
          <Text key={label} inverse={label === filtered[selection]}>{label === filtered[selection] ? "› " : "  "}{label}</Text>)}
      </>}
    <Text dimColor>↑/↓ choose · Enter continue · Esc {pending ? "cancel connection" : "close"}</Text>
  </Box>;
}
