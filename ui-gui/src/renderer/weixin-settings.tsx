import React from "react";
import type { UIEvent } from "@astra/ui-core/session-state";

const STATES: Record<string, string> = {
  off: "未启用", logged_out: "未登录", stopped: "已停止", standby: "待命：另一个 Astra 会话正在接收",
  online: "在线", expired: "登录已失效，请重新扫码",
};

/** What the settings show of the WeChat channel. The backend's latest weixin_status is the only source. */
export function weixinView(status: UIEvent | undefined) {
  const state = typeof status?.state === "string" ? status.state : "";
  const qr = status?.qr && typeof status.qr.svg === "string" && status.qr.svg.startsWith("data:image/svg+xml") ? status.qr.svg : "";
  return {
    known: !!status, enabled: !!status?.enabled, loggedIn: !!status?.logged_in, account: String(status?.account ?? ""),
    state, label: STATES[state] ?? "未知", keepAwake: !!status?.keep_awake,
    waiting: status?.login === "scan" || status?.login === "confirm", confirming: status?.login === "confirm", qr,
    message: typeof status?.message === "string" ? status.message : "", error: typeof status?.error === "string" ? status.error : "",
  };
}

/** The channel belongs to the installation; any open session's backend logs in, switches and reports it. */
export function WeixinSettings({ status, ready, run }: { status: UIEvent | undefined; ready: boolean; run: (command: string) => void }) {
  const view = weixinView(status);
  return <>
    <h3>微信</h3>
    <p className="muted small">通过微信官方的 iLink 接口收发消息，不操作微信客户端，息屏、锁屏时也能回复（电脑不能睡眠）。只回复扫码登录的那个微信号。</p>
    {view.known && <p className="small" role="status">状态：{view.label}{view.account && `（${view.account}）`}{view.state === "online" && (view.keepAwake ? " · 已阻止电脑空闲睡眠" : " · 未阻止睡眠")}</p>}
    {view.waiting && view.qr && <div className="weixin-qr"><img src={view.qr} alt="微信登录二维码" width={192} height={192}/><p className="muted small">{view.confirming ? "已扫码，请在手机微信里确认。" : "用手机微信扫一扫登录。"}</p></div>}
    {view.error && <p className="small error-text" role="alert">{view.error}</p>}
    <div className="weixin-actions">
      <button disabled={!ready || view.waiting} onClick={() => run("/weixin login")}>{view.loggedIn ? "重新扫码登录" : "扫码登录"}</button>
      {view.loggedIn && (view.enabled
        ? <button disabled={!ready} onClick={() => run("/weixin off")}>停用</button>
        : <button disabled={!ready} onClick={() => run("/weixin on")}>启用</button>)}
      {view.loggedIn && <button disabled={!ready} onClick={() => run("/weixin logout")}>退出登录</button>}
    </div>
    {!ready && <p className="muted small">打开一个会话后可以设置。</p>}
  </>;
}
