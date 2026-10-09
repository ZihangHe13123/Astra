import { contextBridge, ipcRenderer, webUtils } from "electron";
import type { DesktopBridge, DesktopEvent } from "../bridge.js";

/** The bridge belongs to the window. This script also runs in every frame below it, where it must expose nothing. */
function exposeBridge() {
  const invoke = (method: string, ...args: unknown[]) => ipcRenderer.invoke(`astra:${method}`, ...args);
  const bridge: DesktopBridge = {
    bootstrap: () => invoke("bootstrap"), create: options => invoke("create", options),
    sessionAction: (action, entry) => invoke("sessionAction", action, entry),
    select: id => invoke("select", id), close: id => invoke("close", id),
    send: (id, command) => invoke("send", id, command),
    query: (method, params, runtime) => invoke("query", method, params, runtime),
    preferences: value => invoke("preferences", value), choose: kind => invoke("choose", kind),
    flushPreferences: value => {
      const result = ipcRenderer.sendSync("astra:flushPreferences", value);
      if (result !== true) throw new Error(String(result));
    },
    clipboardImage: () => invoke("clipboardImage"),
    copyText: text => invoke("copyText", text),
    appshot: (id, action, value) => invoke("appshot", id, action, value),
    droppedPaths: files => files.map(file => webUtils.getPathForFile(file)).filter(Boolean),
    openExternal: url => invoke("openExternal", url), file: (id, path, action, requestId) => invoke("file", id, path, action, requestId),
    cancelPreview: (id, requestId) => invoke("cancelPreview", id, requestId),
    onEvents: handler => {
      const listener = (_: unknown, events: DesktopEvent[]) => handler(events);
      ipcRenderer.on("astra:events", listener);
      return () => ipcRenderer.removeListener("astra:events", listener);
    },
  };
  contextBridge.exposeInMainWorld("astra", bridge);
}

/**
 * A frame below the window is an interactive card, or a frame a card made inside itself. Its
 * content policy stops every request, but WebRTC connects without asking the policy. Those
 * interfaces are removed here, before any script of the frame runs; a fresh frame is the only way
 * to get them back, and this runs in that one too.
 */
function stripPeerConnections() {
  contextBridge.executeInMainWorld({ func: () => {
    for (const name of Object.getOwnPropertyNames(window)) {
      if (/^(?:webkit|moz)?RTC/.test(name)) Reflect.deleteProperty(window, name);
    }
  } });
}

if (window.top === window) exposeBridge();
else stripPeerConnections();
