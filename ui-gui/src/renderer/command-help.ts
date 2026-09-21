import type { CommandDescription } from "../bridge.js";

export type CommandMode = { mode: string; command: string; label: string; description: string };
export type CommandSuggestion = { value: string; label: string; description: string; more: boolean; hint?: string };

export const names: Record<string, string> = {
  image:"添加图片", bar:"酒吧", minimal:"极简模式", sip:"小酌", reset:"清空对话", compress:"压缩上下文",
  undo:"撤销回复", retry:"重试回复", changes:"查看改动", think:"推理显示", model:"选择模型", mode:"推理强度", connect:"连接模型",
  persona:"选择人格", search:"搜索来源", tool:"工具结果", gallery:"图片画廊", memory:"记忆", skills:"技能", learn:"技能学习",
  tools:"可用工具", browser:"浏览器控制", computer:"电脑控制", conclave:"专家研究", appshot:"窗口捕获", tasks:"任务", budget:"时间预算",
  resume:"恢复任务", cancel:"停止任务", goal:"目标", today:"今日任务", session:"会话管理", handoff:"交接", theme:"外观", timeline:"时间线",
  health:"运行健康", doctor:"诊断", diagnostics:"诊断详情", maintenance:"维护", sandbox:"沙箱", "vision-tiles":"图像分块",
  "context-index":"上下文索引", mcp:"MCP 集成", yolo:"权限模式", permissions:"工具权限", reload:"热重载", reconnect:"重新连接",
  restart:"受控重启", wakeup:"会话提醒", help:"帮助",
};
const descriptions: Record<string, string> = {
  image:"选择图片附件，也可输入路径和说明", sip:"喝一口当前酒吧饮品", reset:"清空当前对话的上下文", compress:"用模型摘要压缩当前上下文",
  undo:"撤销最后一条回复；加数字可撤销多轮对话", retry:"移除上一轮回复，重新发送上一次请求", changes:"查看最近各轮的文件改动",
  think:"显示或隐藏模型返回的推理或摘要", model:"查看可用模型并切换", mode:"调整推理强度：low / high / max", connect:"登录 ChatGPT 或配置 API 模型",
  persona:"查看或切换当前人格", search:"选择网页搜索来源", tool:"查看最近或指定编号的工具结果", gallery:"查看最近或指定编号的图片结果",
  memory:"查看、讨论或更新记忆", skills:"浏览、查看或创建本地技能", learn:"讨论技能改进，保存或撤销修改", tools:"列出当前可用工具",
  browser:"查看或释放会话的浏览器控制", computer:"查看、设置或停止电脑控制", conclave:"开展专家研究并讨论结论", appshot:"查看、启用或配置窗口捕获",
  tasks:"查看任务运行记录", budget:"设置本轮时间预算，单位为秒；off 关闭", resume:"恢复指定的中断任务，需要任务 ID", cancel:"停止当前或指定任务",
  goal:"设置、查看、暂停或继续会话目标", today:"查看今天的任务记录", session:"浏览、继续或管理已保存会话", handoff:"整理并保存交接说明",
  theme:"选择界面外观", timeline:"显示或隐藏消息时间", health:"查看运行健康报告", doctor:"讨论诊断与修复；--raw 查看原始报告",
  diagnostics:"解释运行状态；--raw 查看原始快照", maintenance:"预览或执行运行数据维护", sandbox:"查看或切换 Docker 隔离",
  "vision-tiles":"查看或切换 DeepSeek 原始像素图像分块", "context-index":"设置历史上下文建议", mcp:"查看 MCP 服务器与工具状态",
  yolo:"切换工具审批，也可指定 on / off / status", permissions:"打开工具审批与对话模式设置", reload:"重新加载代码、人格、技能或模型配置",
  reconnect:"重新连接已断开的会话后端", restart:"当前工作交付后受控重启后端", wakeup:"查看、取消或设置会话提醒", help:"打开全部命令与功能",
};
const usage: Record<string, string> = {
  "/image": "/image <图片路径> [说明]", "/undo": "/undo [1–50]", "/tool": "/tool [编号]", "/gallery": "/gallery [编号]",
  "/budget": "/budget <秒数|off>", "/resume": "/resume <任务 ID>", "/cancel": "/cancel [任务 ID]",
  "/mode": "/mode <low|high|max>", "/wakeup": "/wakeup status · /wakeup after <秒数> <提示词>",
};

export function commandNames(modes: CommandMode[]): Record<string, string> {
  return { ...names, ...Object.fromEntries(modes.map(mode => [mode.command.slice(1), `${mode.label}模式`])) };
}
function modeActionHelp(action: string, mode: CommandMode): [string, string] | undefined {
  const actions: Record<string, [string, string]> = {
    enter: [`进入${mode.label}`, `继续最近的${mode.label}会话；没有历史时新建`],
    new: [`新建${mode.label}会话`, "另开一段独立对话，已有会话仍然保留"],
    leave: ["返回通用", "离开当前模式，恢复先前的通用工作上下文"],
    status: ["查看模式状态", "显示当前模式与会话信息"],
    sessions: ["历史会话", `浏览并继续已保存的${mode.label}会话`],
    list: ["历史会话", `浏览并继续已保存的${mode.label}会话`],
    sip: ["小酌一口", "喝一口当前饮品，更新酒杯状态"],
    undo: ["撤销回复", "不加数字撤销最后一条回复；加 1–50 撤销相应轮数"],
    retry: ["重试回复", "移除上一轮回复，重新发送上一次请求"],
    output: ["回复呈现方式", "选择整段呈现或边生成边显示"],
    "output atomic": ["整段呈现", "回复验证后，文字和酒杯状态一起更新"],
    "output stream": ["流式呈现", "文字立即显示，场景工具完成后更新状态"],
    "output status": ["查看呈现方式", "显示当前保存的酒吧输出设置"],
  };
  return actions[action];
}

/** Keep registered commands authoritative; remove TUI-only back-navigation rows. */
export function describeCommands(catalog: CommandDescription[], modes: CommandMode[]): CommandDescription[] {
  return catalog.map(command => {
    const mode = modes.find(mode => mode.command === command.command);
    return { ...command, description: mode?.description || descriptions[command.command.slice(1)] || command.description,
      options: command.options.filter(option => option.completion.trim() !== command.command).map(option => {
        const action = option.completion.trim().slice(command.command.length + 1);
        const translated = mode && modeActionHelp(action, mode);
        return translated ? { ...option, description: translated[1] } : option;
      }) };
  });
}

/** Mode history is GUI navigation, including /bar sessions (a TUI submenu). */
export function modeSessionTarget(value: string, modes: CommandMode[]): string | undefined {
  return modes.find(mode => value.trim() === `${mode.command} sessions` || value.trim() === `${mode.command} list`)?.mode;
}

export function commandUsage(value: string, modes: CommandMode[]): string | undefined {
  const root = value.trim().split(/\s+/)[0];
  const mode = modes.find(mode => mode.command === root);
  if (mode) return `${root} new · ${root} leave · ${root} <会话名>`;
  return usage[root];
}

export function suggestCommands(catalog: CommandDescription[], draft: string, modes: CommandMode[], currentMode: string): CommandSuggestion[] {
  if (!draft.startsWith("/") || /[\r\n]/.test(draft)) return [];
  const labels = commandNames(modes);
  const activeMode = modes.find(mode => mode.mode === currentMode);
  const modeHint = (root: string) => activeMode && modes.some(mode => mode.command === root && mode !== activeMode)
    ? `先执行 ${activeMode.command} leave 返回通用模式` : undefined;
  const roots = catalog.map(command => ({ value: command.command, label: labels[command.command.slice(1)] || command.command,
    description: command.description, more: command.options.length > 0, hint: modeHint(command.command) }));
  const options = (command: CommandDescription): CommandSuggestion[] => command.options.map(option => {
    const value = option.completion.trim();
    const mode = modes.find(mode => mode.command === command.command);
    const action = value.slice(command.command.length + 1);
    const translated = mode && modeActionHelp(action, mode);
    return { value, label: translated?.[0] || option.command, description: option.description,
      more: command.options.some(other => other.completion.trim().startsWith(`${value} `)), hint: modeHint(command.command) };
  });
  const text = draft.toLowerCase();
  const root = catalog.find(command => command.command === text.split(/\s+/)[0]);
  if (root) {
    const query = text.slice(root.command.length).trim();
    const children = options(root).filter(option => option.value.toLowerCase().startsWith(text)
      || (query && `${option.label} ${option.description}`.toLowerCase().includes(query)));
    return draft === root.command ? [roots.find(item => item.value === root.command)!, ...children] : children;
  }
  const query = text.slice(1).trim();
  if (query) return roots.filter(item => `${item.value.slice(1)} ${item.label} ${item.description}`.toLowerCase().includes(query));
  const current = catalog.find(command => command.command === activeMode?.command);
  const priority = ["leave", "new", "sessions", "list", "sip", "undo", "retry", "status"];
  const currentOptions = current ? options(current).sort((a, b) => {
    const rank = (item: CommandSuggestion) => { const n = priority.indexOf(item.value.slice(current.command.length + 1)); return n < 0 ? priority.length : n; };
    return rank(a) - rank(b);
  }) : [];
  const common = ["/model", "/permissions", "/session", ...modes.map(mode => mode.command), "/changes", "/compress", "/theme", "/help"];
  return [...currentOptions, ...roots.sort((a, b) => {
    const rank = (item: CommandSuggestion) => { const n = common.indexOf(item.value); return n < 0 ? common.length : n; };
    return rank(a) - rank(b);
  })];
}
