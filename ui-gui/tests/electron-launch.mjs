import { _electron as electron } from 'playwright';
import { fileURLToPath } from 'node:url';

const compatibility = fileURLToPath(new URL('./electron-compatibility.cjs', import.meta.url));

/** Playwright installs its loader first; this preload then restores the affected engine default before the application entry. */
export function launchElectron(options = {}) {
  return electron.launch({ ...options, args: ['-r', compatibility, ...(options.args || [])] });
}
