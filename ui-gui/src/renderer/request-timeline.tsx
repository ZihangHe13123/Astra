import React, { useEffect, useState } from "react";
import type { SessionState } from "@astra/ui-core/session-state";

type Metrics = {
  request_id: string; step: number; model: string; timestamp?: number; total_ms?: number; pre_request_ms?: number;
  request_to_first_event_ms?: number; request_to_first_text_ms?: number; failed?: boolean;
  usage: Record<string, number>; fingerprint: Record<string, number>; phases_ms: Record<string, number>;
  cache?: { status?: string; causes?: string[] };
};
type Timeline = { available: boolean; recording_enabled: boolean; records: Metrics[]; has_more: boolean; scan_truncated: boolean; unscoped_records: number };
const duration = (value?: number) => value === undefined ? "未采集" : value >= 1000 ? `${(value / 1000).toFixed(2)} 秒` : `${Math.round(value)} ms`;
const count = (value?: number) => value === undefined ? "未采集" : value.toLocaleString();
const causeLabels: Record<string, string> = { model: "模型变化", system_prompt: "系统提示变化", tool_schemas: "工具定义变化",
  active_skills: "技能变化", message_prefix: "消息前缀变化", possible_cache_ttl_or_server_eviction: "可能的服务端缓存过期" };

export function RequestMetricsCard({ record: r }: { record: Metrics }) {
  const measured = r.total_ms !== undefined && r.pre_request_ms !== undefined && r.request_to_first_event_ms !== undefined;
  const total = Math.max(r.total_ms || 0, 1);
  const prepare = Math.min(r.pre_request_ms || 0, total);
  const wait = Math.min(r.request_to_first_event_ms || 0, total - prepare);
  return <details className="tool-detail request-metrics">
    <summary><strong>步骤 {r.step} · {r.model || "模型未记录"}</strong><span className="muted small">{duration(r.total_ms)}{r.failed ? " · 失败" : ""}</span></summary>
    <p className="muted small">请求 {r.request_id}{r.timestamp !== undefined ? ` · ${new Date(r.timestamp * 1000).toLocaleString()}` : ""}</p>
    {measured && <div className="request-timing" role="img" aria-label={`请求准备 ${duration(prepare)}，等待首个响应 ${duration(wait)}，首个响应后 ${duration(total - prepare - wait)}`}>
      <span style={{ width: `${prepare / total * 100}%`, background: "#7597c6" }} title={`准备 ${duration(prepare)}`}/>
      <span style={{ width: `${wait / total * 100}%`, background: "#d3b065" }} title={`等待 ${duration(wait)}`}/>
      <span style={{ width: `${(total - prepare - wait) / total * 100}%`, background: "#71a990" }} title={`响应 ${duration(total - prepare - wait)}`}/>
    </div>}
    <dl><dt>请求准备</dt><dd>{duration(r.pre_request_ms)}</dd><dt>等待首个响应</dt><dd>{duration(r.request_to_first_event_ms)}</dd>
      <dt>等待首段文字</dt><dd>{duration(r.request_to_first_text_ms)}</dd><dt>输入 token</dt><dd>{count(r.usage.prompt_tokens)}</dd>
      <dt>输出 token</dt><dd>{count(r.usage.completion_tokens)}</dd><dt>缓存命中</dt><dd>{count(r.usage.prompt_cache_hit_tokens)}</dd>
      <dt>缓存未命中</dt><dd>{count(r.usage.prompt_cache_miss_tokens)}</dd><dt>请求 token（客户端估算）</dt><dd>{count(r.fingerprint.request_tokens_estimate)}</dd>
      <dt>工具数量</dt><dd>{count(r.fingerprint.tool_count)}</dd></dl>
    {r.cache?.status === "material_drop" && <p className="notice">缓存命中下降{r.cache.causes?.length ? `：${r.cache.causes.map(c => causeLabels[c] || c).join("、")}` : ""}</p>}
    {!!Object.keys(r.phases_ms).length && <details><summary>阶段详情</summary><dl>{Object.entries(r.phases_ms).map(([name, value]) => <React.Fragment key={name}><dt>{name}</dt><dd>{duration(value)}</dd></React.Fragment>)}</dl></details>}
  </details>;
}

/** Recent request metrics use a read-only worker; paging keeps at most 25 cards mounted. */
export function RequestTimeline({ state }: { state: SessionState }) {
  const [result, setResult] = useState<Timeline>();
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const [page, setPage] = useState(0);
  useEffect(() => {
    let disposed = false;
    void window.astra.query("request_timeline", { name: state.session, mode: state.mode, limit: 200 }).then(value => {
      if (!disposed) { setResult(value); setError(""); }
    }).catch(cause => { if (!disposed) setError(String(cause)); });
    return () => { disposed = true; };
  }, [state.session, state.mode, state.busy, state.info.usage, refresh]);
  const pages = Math.max(1, Math.ceil((result?.records.length || 0) / 25));
  const current = Math.min(page, pages - 1);
  return <section className="request-timeline" aria-label="请求执行时间线">
    <div className="actions"><h4>请求执行时间线</h4><button onClick={() => setRefresh(x => x + 1)}>刷新记录</button></div>
    <p className="muted small">显示已记录的请求耗时与用量。工具执行见“执行”页；未采集的指标保持空缺。</p>
    {error && <p className="error-text">{error}</p>}
    {!result && !error && <p className="muted">正在读取记录…</p>}
    {result && !result.records.length && <p className="muted">此会话暂无请求记录。{result.recording_enabled ? "后续请求完成后会显示耗时与用量。" : "请求指标采集已关闭。"}</p>}
    {result?.records.slice(current * 25, (current + 1) * 25).map(r => <RequestMetricsCard key={`${r.request_id}:${r.step}`} record={r}/>)}
    {pages > 1 && <div className="actions"><button disabled={current === 0} onClick={() => setPage(current - 1)}>较新记录</button><span>{current + 1} / {pages}</span><button disabled={current + 1 === pages} onClick={() => setPage(current + 1)}>较早记录</button></div>}
    {!!result?.unscoped_records && <p className="muted small">部分旧记录缺少会话归属，未计入当前会话。</p>}
    {(result?.has_more || result?.scan_truncated) && <p className="muted small">仅显示最近的有界记录；更早的指标可能已轮转。</p>}
  </section>;
}
