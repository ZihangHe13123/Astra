import type { CommandDescription } from "./bridge.js";

/** Public installs discover optional modes from their trusted local backend. */
type LocalDefinition = { command: string; label: string; description: string } | null | undefined;
export function modeCommand(mode: string, local: LocalDefinition): string | undefined {
  if (mode === "local") return local?.command;
  if (mode === "minimal" || mode === "bar") return `/${mode}`;
  return undefined;
}
export function modeChoices(local: LocalDefinition) {
  return [
    { mode: "work", label: "通用", description: "完整工具与记忆" },
    { mode: "minimal", label: "极简", description: "独立的轻量对话" },
    { mode: "bar", label: "酒吧", description: "进入酒吧场景" },
    ...(local ? [{ mode: "local", label: local.label, description: local.description }] : []),
  ];
}
export function localCommandEntry(local: LocalDefinition): CommandDescription[] {
  if (!local) return [];
  return [{ id: local.command.slice(1), command: local.command, description: local.description,
    takes_args: true, group: "CHAT", options: [
      { command: "new", description: `新建 ${local.label} 会话`, completion: `${local.command} new`, submitValue: `${local.command} new` },
      { command: "list", description: "列出本地模式会话", completion: `${local.command} list`, submitValue: `${local.command} list` },
      { command: "leave", description: "返回通用模式", completion: `${local.command} leave`, submitValue: `${local.command} leave` },
    ] }];
}
