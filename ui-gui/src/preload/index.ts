import { contextBridge, ipcRenderer, webUtils } from "electron";
import type { DesktopBridge, DesktopEvent } from "../bridge.js";

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
  appshot: (id, action, value) => invoke("appshot", id, action, value),
  droppedPaths: files => files.map(file => webUtils.getPathForFile(file)).filter(Boolean),
  openExternal: url => invoke("openExternal", url), file: (id, path, action) => invoke("file", id, path, action),
  onEvents: handler => {
    const listener = (_: unknown, events: DesktopEvent[]) => handler(events);
    ipcRenderer.on("astra:events", listener);
    return () => ipcRenderer.removeListener("astra:events", listener);
  },
};
contextBridge.exposeInMainWorld("astra", bridge);
