import React from "react";
import { tokenLabel } from "./usage-view.js";

export type UsageDay = {
  day: string; tokens: number; cost: number | null; currency: string; unpriced: string[];
  models: { model: string; prompt: number; completion: number; cached: number; requests: number; cost: number | null }[];
  sources: Record<string, number>; budget: { daily_tokens?: number; daily_cost?: number }; over_budget: boolean;
};

const SOURCES: Record<string, string> = { chat: "对话", channel: "微信/QQ", job: "定时任务" };
const money = (value: number | null | undefined, currency: string) => value == null ? "" : `${currency}${value < 0.01 ? value.toFixed(4) : value.toFixed(2)}`;

/** One model's per-million prices: entered once, then its cost shows beside its tokens. */
function PriceForm({ model, busy, save }: { model: string; busy: boolean; save: (model: string, values: string[]) => Promise<void> }) {
  const [open, setOpen] = React.useState(false);
  const [values, setValues] = React.useState(["", "", ""]);
  if (!open) return <button className="link small" onClick={() => setOpen(true)}>填单价</button>;
  const set = (index: number, value: string) => setValues(values.map((old, i) => i === index ? value : old));
  return <span className="usage-price small">
    {["输入", "输出", "缓存命中"].map((label, index) => <input key={label} aria-label={`${model} ${label}单价`} placeholder={label}
      inputMode="decimal" value={values[index]} onChange={event => set(index, event.target.value)}/>)}
    <span className="muted">每百万 token</span>
    <button disabled={busy || !values[0] || !values[1]} onClick={() => { void save(model, values.filter(Boolean)).then(() => setOpen(false)); }}>保存</button>
  </span>;
}

/** Today's model usage on the activity page: by model and source, against the person's daily budget. */
export function UsagePanel({ usage, fail, reload }: { usage: UsageDay | null | undefined; fail: (error: unknown) => void; reload: () => Promise<void> }) {
  const [budget, setBudget] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const change = async (action: "budget" | "price", ...values: string[]) => {
    setBusy(true);
    try { await window.astra.usage(action, ...values); await reload(); } catch (error) { fail(error); }
    finally { setBusy(false); }
  };
  const currency = usage?.currency || "¥";
  const limit = usage?.budget.daily_cost ? money(usage.budget.daily_cost, currency) : usage?.budget.daily_tokens ? `${tokenLabel(usage.budget.daily_tokens)} token` : "";
  return <section className="usage-panel">
    <h3>今日用量</h3>
    {!usage || !usage.models.length ? <p className="muted small">今天还没有模型请求。</p> : <>
      <p className={usage.over_budget ? "error-text" : ""}><strong>{tokenLabel(usage.tokens)} token</strong>{usage.cost != null && `，约 ${money(usage.cost, currency)}`}
        {limit && ` · 每日预算 ${limit}`}{usage.over_budget && "，已超过：定时检查暂停到明天"}</p>
      <ul className="usage-models small">{usage.models.map(entry => <li key={entry.model}>
        <strong>{entry.model}</strong>：输入 {tokenLabel(entry.prompt)}（缓存命中 {tokenLabel(entry.cached)}），输出 {tokenLabel(entry.completion)}，{entry.requests} 次
        {entry.cost != null ? `，约 ${money(entry.cost, currency)}` : <> <PriceForm model={entry.model} busy={busy} save={(model, values) => change("price", model, ...values)}/></>}
      </li>)}</ul>
      <p className="muted small">来源：{Object.entries(usage.sources).sort((a, b) => b[1] - a[1]).map(([source, tokens]) => `${SOURCES[source] || source} ${tokenLabel(tokens)}`).join("，")}</p>
    </>}
    <div className="memory-actions small">
      <input aria-label="每日预算" placeholder={limit ? `现在：${limit}` : "每日预算：500000 或 ¥5"} value={budget} onChange={event => setBudget(event.target.value)}/>
      <button disabled={busy || !budget.trim()} onClick={() => { void change("budget", budget.trim()).then(() => setBudget("")); }}>设预算</button>
      {limit && <button disabled={busy} onClick={() => { void change("budget", "off"); }}>关闭预算</button>}
    </div>
    <p className="muted small">超过预算后，当天剩下的定时检查不运行，第二天恢复；对话照常。金额要先给模型填单价。</p>
  </section>;
}
