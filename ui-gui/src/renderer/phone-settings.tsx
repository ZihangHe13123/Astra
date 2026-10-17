import React, { useEffect, useState } from "react";
import type { PhoneAction, PhoneStatus } from "../phone-protocol.js";

const time = (iso?: string) => iso ? new Date(iso).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }) : "从未";

/** The phone section of Settings. The desktop process owns devices and the gateway; this only shows and asks. */
export function PhoneSettingsView({ status, run, now = Date.now() }: {
  status: PhoneStatus | undefined; run: (action: PhoneAction, value?: string) => void; now?: number;
}) {
  if (!status) return <><h3>手机</h3><p className="muted small">正在读取…</p></>;
  const seconds = status.pairing ? Math.max(0, Math.round((status.pairing.expiresAt - now) / 1000)) : 0;
  return <>
    <h3>手机</h3>
    <p className="muted small">已配对的 iPhone 经 Tailscale 查看这台 Mac 上打开的会话。只有本机监听，外网连不进来。</p>
    <label className="option"><input type="checkbox" checked={status.enabled} onChange={event => run(event.target.checked ? "enable" : "disable")}/>允许配对的手机连接</label>
    {status.enabled && <>
      {status.error ? <p className="error-text small" role="alert">{status.error}</p>
        : <p className="muted small">在终端运行一次 <code>tailscale serve --bg {status.port}</code>，再在 iPhone 的 Safari 打开
          {status.url ? <> <code>{status.url}</code></> : " 这台 Mac 的 Tailscale 地址"}，分享 → 添加到主屏幕，从主屏幕打开后配对。</p>}
      {status.pairing && seconds > 0
        ? <div className="phone-pairing" role="status"><span className="muted small">在手机上输入配对码（{Math.floor(seconds / 60)}:{String(seconds % 60).padStart(2, "0")} 内有效）</span>
          <strong className="phone-code">{status.pairing.code.slice(0, 4)} {status.pairing.code.slice(4)}</strong></div>
        : <button onClick={() => run("pair")}>添加手机</button>}
      {status.devices.length > 0 && <ul className="phone-devices">{status.devices.map(device =>
        <li key={device.id}><span><strong>{device.name}</strong><small className="muted">配对于 {time(device.createdAt)} · 最近连接 {time(device.lastSeen)}</small></span>
          <button onClick={() => run("revoke", device.id)}>移除</button></li>)}</ul>}
    </>}
  </>;
}

/** Keeps the status fresh while Settings is open: a phone that pairs shows up without reopening the panel. */
export function PhoneSettings({ phone }: { phone: (action: PhoneAction, value?: string) => Promise<PhoneStatus> }) {
  const [status, setStatus] = useState<PhoneStatus>();
  const [error, setError] = useState("");
  const [now, setNow] = useState(Date.now());
  const run = (action: PhoneAction, value?: string) => {
    // The switch moves when clicked; the desktop's answer then confirms it, or a re-read puts it back.
    if (action === "enable" || action === "disable") setStatus(current => current && { ...current, enabled: action === "enable" });
    void phone(action, value).then(next => { setStatus(next); setError(""); }).catch(cause => {
      setError(cause instanceof Error ? cause.message : String(cause));
      void phone("status").then(setStatus).catch(() => undefined);
    });
  };
  useEffect(() => { run("status"); }, []);
  useEffect(() => {
    if (!status?.pairing) return;
    const timer = setInterval(() => { setNow(Date.now()); run("status"); }, 1000);
    return () => clearInterval(timer);
  }, [status?.pairing?.code]);
  return <>{error && <p className="error-text small" role="alert">{error}</p>}<PhoneSettingsView status={status} run={run} now={now}/></>;
}
