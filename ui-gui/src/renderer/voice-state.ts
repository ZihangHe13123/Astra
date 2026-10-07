import type { UIEvent } from "@astra/ui-core/session-state";
import type { CommandDescription } from "../bridge.js";

/** Spoken replies as the desktop shows them. The backend's latest voice_status is the only source. */
export type VoiceView = {
  configured: boolean; enabled: boolean; activity?: "starting" | "speaking";
  voice: string; voices: string[]; error: string;
};
export function voiceView(status: UIEvent | undefined): VoiceView {
  return { configured: !!status?.configured, enabled: !!status?.enabled,
    activity: status?.state === "starting" || status?.state === "speaking" ? status.state : undefined,
    voice: typeof status?.voice === "string" ? status.voice : "",
    voices: Array.isArray(status?.voices) ? status.voices.filter((name: unknown): name is string => typeof name === "string") : [],
    error: typeof status?.error === "string" ? status.error : "" };
}

/** The composer's voice button. An installation without a speech endpoint has none. */
export function voiceButton(status: UIEvent | undefined): { label: string; on: boolean } | undefined {
  const voice = voiceView(status);
  if (voice.enabled) return { label: voice.voice ? `朗读 · ${voice.voice}` : "朗读已开启", on: true };
  return voice.configured ? { label: "朗读已关闭", on: false } : undefined;
}

/** The line above the composer: what speech is doing, or why it is not happening. */
export function voiceNotice(status: UIEvent | undefined): { text: string; action: "stop" | "settings" } | undefined {
  const voice = voiceView(status);
  if (voice.activity) return { text: voice.activity === "starting" ? "正在准备朗读…" : "正在朗读回复。", action: "stop" };
  if (voice.enabled && !voice.configured) return { text: "朗读已开启，但还没有配置语音服务。", action: "settings" };
  // A command's own failure is answered where it was given; this line is for speech that failed by itself.
  if (voice.error && status?.message === undefined) return { text: `朗读没有成功：${voice.error}`, action: "settings" };
  return undefined;
}

/** The answer to a /voice command the user typed, told from the state the backend reports back. */
export function voiceAnswer(action: string, status: UIEvent): string {
  const voice = voiceView(status);
  // A command that was carried out is acknowledged with a message; one that was not has only its error.
  if (!status.message) return `朗读：${voice.error || "命令没有执行"}`;
  if (action === "use") return `音色已切换为 ${voice.voice}`;
  if (action === "test") return "正在试听当前音色";
  if (action === "stop") return "已停止朗读";
  return voice.enabled ? `朗读已开启${voice.voice ? ` · ${voice.voice}` : ""}` : "朗读已关闭";
}

/** The backend splits arguments like a shell, so a name with spaces or quotes travels quoted. */
export function voiceUseCommand(name: string): string {
  return `/voice use ${/[\s"'\\]/.test(name) ? `"${name.replace(/[\\"]/g, "\\$&")}"` : name}`;
}

/** Offer each defined voice under /voice, next to the registered actions. */
export function withVoiceOptions(command: CommandDescription, status: UIEvent | undefined): CommandDescription {
  const voice = voiceView(status);
  return { ...command, options: [...command.options, ...voice.voices.map(name => ({
    command: name, description: name === voice.voice ? "当前音色" : "切换到这个音色", completion: voiceUseCommand(name) }))] };
}
