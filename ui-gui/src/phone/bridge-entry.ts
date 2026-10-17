import { RemoteBridge } from "./bridge.js";
import { indexedDbStorage, PhoneClient, type PhoneDeviceRecord } from "./client.js";

/**
 * Loaded before the desktop renderer on a paired phone (src/main/phone-gateway.ts injects it), so
 * `window.astra` is the remote bridge by the time the app starts.
 */
const client = new PhoneClient(indexedDbStorage());
const PREFERENCES = "astra-phone-preferences";

const STYLE = `
.astra-pair { position: fixed; inset: 0; z-index: 2147483647; display: flex; flex-direction: column; gap: 14px;
  padding: calc(env(safe-area-inset-top) + 28px) 20px 20px; background: #f6f6f4; color: #1b1b1f;
  font: 16px/1.5 -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif; overflow: auto; }
.astra-pair h1 { font-size: 24px; margin: 0; }
.astra-pair p { margin: 0; color: #6b6b73; }
.astra-pair .notice { color: #1b1b1f; background: #eef1fb; padding: 10px 12px; border-radius: 10px; }
.astra-pair label { display: flex; flex-direction: column; gap: 6px; font-size: 14px; color: #6b6b73; }
.astra-pair input { font: inherit; font-size: 20px; padding: 12px; border-radius: 10px; border: 1px solid #d9d9d4;
  background: #fff; color: #1b1b1f; letter-spacing: 0.08em; }
.astra-pair button { font: inherit; font-weight: 600; padding: 13px; border: 0; border-radius: 10px; background: #3b5bdb; color: #fff; }
.astra-pair button:disabled { opacity: 0.5; }
.astra-pair .error { color: #d9480f; }
@media (prefers-color-scheme: dark) {
  .astra-pair { background: #18181b; color: #ececf0; }
  .astra-pair input { background: #222226; color: #ececf0; border-color: #34343a; }
  .astra-pair .notice { background: #262a3a; color: #ececf0; }
}`;

function pairingScreen(notice?: string): Promise<PhoneDeviceRecord> {
  return new Promise(resolve => {
    const show = () => {
      const style = document.createElement("style"); style.textContent = STYLE;
      const form = document.createElement("form"); form.className = "astra-pair";
      form.innerHTML = `<h1>连接 Mac 上的 Astra</h1>${notice ? `<p class="notice"></p>` : ""}
        <p>在 Mac 上打开 Astra 的“设置 → 手机”，点“添加手机”，输入显示的配对码。</p>
        <label>配对码<input name="code" autocapitalize="characters" autocomplete="one-time-code" autocorrect="off" spellcheck="false" maxlength="12" required></label>
        <label>这台手机的名字<input name="name" value="iPhone" maxlength="60"></label>
        <p class="error" role="alert"></p><button>配对</button>`;
      if (notice) form.querySelector(".notice")!.textContent = notice;
      const code = form.querySelector<HTMLInputElement>('input[name="code"]')!;
      const name = form.querySelector<HTMLInputElement>('input[name="name"]')!;
      const button = form.querySelector("button")!;
      const error = form.querySelector<HTMLElement>(".error")!;
      code.addEventListener("input", () => { code.value = code.value.toUpperCase(); });
      form.addEventListener("submit", event => {
        event.preventDefault(); button.disabled = true; button.textContent = "正在配对…"; error.textContent = "";
        client.pair(code.value, name.value).then(record => { form.remove(); style.remove(); resolve(record); }).catch(cause => {
          error.textContent = cause instanceof Error ? cause.message : String(cause);
          button.disabled = false; button.textContent = "配对";
        });
      });
      document.head.append(style); document.body.append(form); code.focus();
    };
    if (document.body) show(); else document.addEventListener("DOMContentLoaded", show, { once: true });
  });
}

const bridge = new RemoteBridge({
  client,
  load: () => { try { return localStorage.getItem(PREFERENCES); } catch { return null; } },
  save: text => { try { localStorage.setItem(PREFERENCES, text); } catch { /* private mode: preferences last this visit */ } },
  pair: pairingScreen,
  reload: () => location.reload(),
  copy: text => navigator.clipboard.writeText(text),
  open: url => { window.open(url, "_blank", "noopener,noreferrer"); },
  sleep: ms => new Promise(resolve => setTimeout(resolve, ms)),
});
window.astra = bridge;
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") bridge.wake(); });
document.documentElement.dataset.remote = "phone";
// iOS ignores user-scalable=no for pinching; its gesture events are the way to stop it.
for (const name of ["gesturestart", "gesturechange"]) document.addEventListener(name, event => event.preventDefault(), { passive: false });
// The keyboard shrinks only the visual viewport on iOS: the app follows it, so the composer sits on the keyboard
// instead of the page being pushed up under it.
const viewport = window.visualViewport;
const fit = () => {
  document.documentElement.style.setProperty("--app-height", `${Math.round(viewport?.height ?? window.innerHeight)}px`);
  if (window.scrollY) window.scrollTo(0, 0);
};
viewport?.addEventListener("resize", fit); viewport?.addEventListener("scroll", fit); window.addEventListener("orientationchange", fit);
fit();
if ("serviceWorker" in navigator) void navigator.serviceWorker.register("./sw.js").catch(() => undefined);
