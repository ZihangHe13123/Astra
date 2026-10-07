import React from "react";
import type { UIEvent } from "@astra/ui-core/session-state";
import { Modal } from "./controls.js";
import { voiceUseCommand, voiceView } from "./voice-state.js";

/** Voice settings belong to the installation. The session's backend applies each choice and reports the result. */
export function VoiceSettings({ status, ready, run, close }: {
  status: UIEvent | undefined; ready: boolean; run: (command: string) => void; close: () => void;
}) {
  const voice = voiceView(status);
  return <Modal title="语音朗读" close={close}><div className="form">
    <p className="muted small">回复生成时读出正文，不读代码、表格、链接和工具输出。设置对这台电脑上的所有会话生效。</p>
    {!voice.configured && <p className="muted small" role="note">还没有配置语音服务：在 Astra 的 settings.json 里添加 voice.base_url，指向任何兼容 OpenAI 语音接口的服务，本地模型或在线 API 都可以。步骤见文档「语音朗读」。</p>}
    <h3>朗读回复</h3>
    <div className="choice-grid">{[{ on: false, name: "关闭", description: "不朗读回复" }, { on: true, name: "开启", description: "回复生成时自动朗读" }].map(option =>
      <button key={option.name} disabled={!ready} aria-pressed={voice.enabled === option.on} onClick={() => run(`/voice ${option.on ? "on" : "off"}`)}><strong>{option.name}</strong><small>{option.description}</small></button>)}</div>
    <h3>音色</h3>
    {voice.voices.length ? <div className="choice-grid">{voice.voices.map(name =>
      <button key={name} disabled={!ready} aria-pressed={name === voice.voice} onClick={() => run(voiceUseCommand(name))}><strong>{name}</strong><small>{name === voice.voice ? "当前音色" : "切换到这个音色"}</small></button>)}</div>
      : <p className="muted small">没有定义具名音色，使用语音服务的默认音色。</p>}
    {/* One button for both, so stopping does not remove the focused control and strand the keyboard outside the dialog. */}
    <div className="voice-actions"><button disabled={!ready || !voice.activity && !voice.configured} onClick={() => run(voice.activity ? "/voice stop" : "/voice test")}>{voice.activity ? "停止朗读" : "试听当前音色"}</button>
      <span className="muted small" role="status">{voice.activity === "starting" ? "正在准备朗读…" : voice.activity === "speaking" ? "正在朗读" : ""}</span></div>
    {voice.error && <p className="error-text small" role="alert">{voice.error}</p>}
  </div></Modal>;
}
