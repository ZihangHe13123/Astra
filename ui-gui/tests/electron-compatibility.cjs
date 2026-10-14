// Playwright 1.59.1 disables RenderDocument for microsoft/playwright#37714.
// In Electron 44.4.3 / Chromium 152.0.7977.130, that override reproducibly crashes
// native frame navigation at FindEmptyBackgroundHostForReuse's foreground CHECK.
// Restore the production default for this confirmed version pair only. Preserve
// every other automation flag; sandbox, CSP and isolation are unchanged.
// https://github.com/microsoft/playwright/blob/v1.59.1/packages/playwright-core/src/server/chromium/chromiumSwitches.ts
// https://github.com/chromium/chromium/blob/152.0.7977.130/content/browser/renderer_host/render_process_host_impl.cc#L1361
if (process.versions.electron === '44.4.3' && process.versions.chrome === '152.0.7977.130') {
  const { app } = require('electron');
  const disabled = app.commandLine.getSwitchValue('disable-features').split(',');
  if (disabled.includes('RenderDocument')) {
    const kept = disabled.filter(feature => feature !== 'RenderDocument').join(',');
    if (kept) app.commandLine.appendSwitch('disable-features', kept);
    else app.commandLine.removeSwitch('disable-features');
  }
}
