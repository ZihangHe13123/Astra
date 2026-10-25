/** What the jobs and activity pages show, from the backend's read-only `jobs` and `activity` queries. */

export type JobRow = {
  id: string; name: string; kind: string; state: string; paused_reason: string; schedule: string;
  next_at: number | null; notify: string; web: boolean; mail?: boolean; prompt: string; targets: string[]; last_outcome: string;
};
export type InboxRow = {
  id: string; job_id: string; name: string; kind: string; state: string; quiet: boolean; output: string; detail: string;
  skipped_count: number; usage: Record<string, number>; time: number; read: boolean; sent: string[];
};
export type JobsOverview = { jobs: JobRow[]; inbox: InboxRow[]; quiet_hours: { start: string; end: string } | null };
export type ActivityEntry = {
  kind: "turn" | "job"; id: string; source: string; time: number; title: string; status: string;
  session?: string; effects?: { name: string; risk: string; status: string; summary: string }[];
  approvals?: { title: string; decision: string }[]; job_id?: string; quiet?: boolean; summary?: string;
};

const pad = (value: number) => String(value).padStart(2, "0");

function sameDay(a: Date, b: Date) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
}

/** "今天" / "昨天" / "10月24日", for grouping and for times on other days. */
export function dayLabel(seconds: number, now = Date.now()) {
  const when = new Date(seconds * 1000);
  const today = new Date(now);
  if (sameDay(when, today)) return "今天";
  if (sameDay(when, new Date(now - 86_400_000))) return "昨天";
  if (sameDay(when, new Date(now + 86_400_000))) return "明天";
  return `${when.getMonth() + 1}月${when.getDate()}日`;
}

export function timeLabel(seconds: number, now = Date.now()) {
  const when = new Date(seconds * 1000);
  return `${dayLabel(seconds, now)} ${pad(when.getHours())}:${pad(when.getMinutes())}`;
}

export function jobState(job: JobRow): { label: string; tone: "on" | "off" | "warn" } {
  if (job.state === "enabled") return { label: "启用中", tone: "on" };
  if (job.state === "completed") return { label: "已完成", tone: "off" };
  if (job.paused_reason === "failures") return { label: "已暂停：连续 3 次没能完成", tone: "warn" };
  if (job.paused_reason === "inactive") return { label: "已暂停：7 天没有互动", tone: "warn" };
  return { label: "已暂停", tone: "off" };
}

const TARGETS: Record<string, string> = { desktop: "桌面通知", weixin: "微信" };

export function targetsLabel(targets: string[]) {
  const names = targets.map(target => TARGETS[target] || target);
  return names.length ? `发到${names.join("和")}` : "只进收件箱";
}

/** One inbox row's headline: what happened, before its text. */
export function resultLabel(row: InboxRow) {
  if (row.state === "completed") return row.quiet ? "没有新内容" : row.kind === "reminder" ? "提醒" : "结果";
  if (row.state === "skipped") return row.skipped_count ? `错过 ${row.skipped_count} 次` : "已暂停";
  return row.state === "unknown" ? "结果未知" : "没能完成";
}

export function resultText(row: InboxRow) {
  if (row.state === "completed") return row.quiet ? "和上次相比没有变化，没有打扰你。" : row.output;
  return row.detail || row.output;
}

const SOURCES: Record<string, string> = { desktop: "桌面", terminal: "终端", weixin: "微信", qq: "QQ", onebot: "QQ", job: "定时任务" };

export function sourceLabel(source: string) {
  return SOURCES[source] || "对话";
}

const TOOLS: Record<string, string> = {
  execute_shell: "执行命令", execute_python: "运行 Python", write_file: "写文件", edit_file: "改文件", apply_patch: "改文件",
  computer_act: "操作电脑", computer_focus: "切换应用", browser_click: "点击网页", browser_fill: "填写网页", browser_type: "网页输入",
  channel_send_file: "发送文件", memory: "改记忆", jobs: "定时任务", doc_edit: "编辑文档", skill_manage: "改技能",
};

export function toolLabel(name: string) {
  return TOOLS[name] || name;
}

/** Entries grouped by day, newest first, as the activity page lists them. */
export function groupByDay<T extends { time: number }>(entries: T[], now = Date.now()) {
  const groups: { label: string; entries: T[] }[] = [];
  for (const entry of [...entries].sort((a, b) => b.time - a.time)) {
    const label = dayLabel(entry.time, now);
    const last = groups.at(-1);
    if (last?.label === label) last.entries.push(entry); else groups.push({ label, entries: [entry] });
  }
  return groups;
}
