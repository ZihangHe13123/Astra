import React from "react";
import { Command, SlidersHorizontal } from "lucide-react";
import { THEMES, isThemeName } from "@astra/ui-core/themes";
import type { UIEvent } from "@astra/ui-core/session-state";
import { useTheme } from "./theme-context.js";
import { ThemeMark } from "./theme-mark.js";

/** The first screen of a conversation. A theme with its own voice opens the way its terminal console
 * does: its mark, what the runtime has loaded, and the commands to start from. */
export function Welcome({ info, openModels, showCommands, run }: {
  info?: UIEvent; openModels: () => void; showCommands: (query?: string) => void; run: (command: string) => void;
}) {
  const theme = useTheme();
  if (!theme.console || !isThemeName(theme.name)) return <section className="welcome"><div className="welcome-mark">A</div><h1>从一个想法开始。</h1><p>对话、研究、编写代码，或把手头的事情交给 Astra。</p><div className="welcome-actions"><button onClick={openModels}><SlidersHorizontal size={16}/>连接或选择模型</button><button onClick={() => showCommands()}><Command size={16}/>浏览全部功能</button></div></section>;
  const words = THEMES[theme.name].console;
  const servers = Array.isArray(info?.mcp) ? info.mcp.filter((server: UIEvent) => server.state !== "disabled") : [];
  const failing = servers.filter((server: UIEvent) => server.state === "error").length;
  const learning = info?.learning;
  const rows: { label: string; value: string; state: string; tone: string }[] = info ? [
    { label: "TOOLS", value: String(info.tools ?? 0), state: words.toolState, tone: "ok" },
    { label: "SKILLS", value: String(info.skills ?? 0), state: words.skillState, tone: "ok" },
    // No server configured is not a link; say so instead of "0/0 linked".
    { label: "MCP", value: servers.length ? `${servers.filter((server: UIEvent) => server.state === "ready").length}/${servers.length}` : "0",
      state: !servers.length ? "NONE SET" : failing ? words.mcpWarningState : words.mcpState, tone: !servers.length ? "plain" : failing ? "warn" : "ok" },
    { label: "LEARN", value: learning?.error ? "?" : String(learning?.learned ?? 0),
      state: learning?.error ? "CHECK /learn" : learning?.mode === "off" ? "OFF" : learning?.pending ? `${learning.pending} TO REVIEW` : "MANUAL REVIEW", tone: learning?.error ? "warn" : learning?.pending ? "value" : "plain" },
  ] : [];
  return <section className="welcome console">
    <div className="welcome-lead">
      <ThemeMark className="welcome-art"/>
      <h1>从一个想法开始。<i className="welcome-caret" aria-hidden="true"/></h1>
      <p>对话、研究、编写代码，或把手头的事情交给 Astra。</p>
    </div>
    {rows.length > 0 && <div className="welcome-matrix">
      <h2>{words.runtimeTitle}</h2>
      {rows.map(row => <div key={row.label} className={`welcome-row ${row.tone}`}><span>{row.label}</span><b>{row.value}</b><i>{row.state}</i></div>)}
      {info?.model && <div className="welcome-row model"><span>MODEL</span><em>{String(info.model)}</em></div>}
    </div>}
    <div className="welcome-routes">
      <h2>{words.commandTitle}</h2>
      <div>
        <button onClick={openModels}><code>/model</code><span>连接或选择模型</span></button>
        <button onClick={() => run("/skills")}><code>/skills</code><span>浏览技能</span></button>
        <button onClick={() => showCommands()}><code>/help</code><span>浏览全部功能</span></button>
      </div>
    </div>
  </section>;
}
