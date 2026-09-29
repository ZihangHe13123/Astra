import { spawn } from 'node:child_process';
import { existsSync, readFileSync, realpathSync } from 'node:fs';
import { homedir } from 'node:os';
import { basename, dirname, isAbsolute, join, relative, resolve } from 'node:path';

export type PackagedRuntime = { root: string; python: string; data: string };
type Options = { isPackaged: boolean; resourcesPath: string; appData: string; platform: string; arch: string };
const inside = (base: string, path: string) => { const part = relative(base, path); return !part.startsWith('..') && !isAbsolute(part); };
function canonicalFuturePath(path: string): string {
  let ancestor = path; const tail: string[] = [];
  while (!existsSync(ancestor)) { tail.unshift(basename(ancestor)); ancestor = dirname(ancestor); }
  return join(realpathSync(ancestor), ...tail);
}

/** Startup checks identity and fixed entry points; full hash checks belong to packaging/staging. */
export function configurePackagedRuntime(options: Options, env: NodeJS.ProcessEnv = process.env): PackagedRuntime | undefined {
  if (!options.isPackaged) return undefined;
  const resource = realpathSync(options.resourcesPath);
  const application = resolve(resource, options.platform === 'darwin' ? '../..' : '..');
  const runtime = join(resource, 'runtime');
  const descriptor = JSON.parse(readFileSync(join(runtime, 'runtime.json'), 'utf8'));
  if (descriptor.schema !== 1 || descriptor.distribution !== 'astra-desktop'
      || descriptor.target !== `${options.platform}-${options.arch}`) throw new Error('Desktop runtime target mismatch');
  const entry = (value: string) => {
    if (typeof value !== 'string' || isAbsolute(value)) throw new Error('Invalid desktop runtime entry');
    const path = realpathSync(join(runtime, value));
    if (!inside(runtime, path)) throw new Error('Desktop runtime entry escaped its bundle');
    return path;
  };
  const root = entry('backend');
  const python = entry(descriptor.python);
  const appData = options.platform === 'win32' ? env.LOCALAPPDATA || options.appData : options.appData;
  const data = resolve(env.ASTRA_HOME || join(appData, 'Astra'));
  // A user may select an external profile, but app resources are immutable.
  if (inside(application, canonicalFuturePath(data))) throw new Error('Desktop user data must be outside the application');
  delete env.PYTHONHOME; delete env.PYTHONPATH; delete env.VIRTUAL_ENV;
  delete env.ELECTRON_RUN_AS_NODE;
  Object.assign(env, {
    AGENT_PROJECT_ROOT: root, ASTRA_INSTALL_ROOT: root, AGENT_PYTHON: python,
    ASTRA_DESKTOP_RUNTIME: '1',
    ASTRA_HOME: data, AGENT_SESSION_DIR: join(data, 'sessions'), ASTRA_ENV_FILE: join(data, '.env'),
    AGENT_LOG_DIR: join(data, 'logs'), ASTRA_WORKSPACE: env.ASTRA_WORKSPACE || homedir(),
    PYTHONPATH: root, PYTHONNOUSERSITE: '1', PYTHONDONTWRITEBYTECODE: '1', PYTHONUNBUFFERED: '1',
    ASTRA_COMPUTER_HELPER_PATH: descriptor.helper ? entry(descriptor.helper) : join(runtime, 'native/unavailable'),
  });
  return { root, python, data };
}

/** Keep the existing Python installation lease alive even with no chat backend open. */
export async function acquirePackagedLease(runtime?: PackagedRuntime, options: { graceMs?: number; stopMs?: number; onLost?: (error: Error) => void } = {}): Promise<{ close(): Promise<void> }> {
  if (!runtime) return { async close() {} };
  const child = spawn(runtime.python, ['-m', 'agent.launcher.desktop_runtime', '--lease'], {
    cwd: runtime.root, env: process.env, windowsHide: true, stdio: ['pipe', 'pipe', 'pipe'],
  });
  let diagnostics = '';
  child.stderr.on('data', chunk => { diagnostics = (diagnostics + chunk).slice(-4000); });
  const ended = new Promise<void>(resolve => child.once('close', () => resolve()));
  child.stdin.on('error', () => { /* child exit is handled by the close receipt */ });
  let closing: Promise<void> | undefined;
  const close = () => closing ||= (async () => {
    const terminate = setTimeout(() => child.kill(), options.graceMs ?? 2_000);
    const kill = setTimeout(() => child.kill('SIGKILL'), (options.graceMs ?? 2_000) + (options.stopMs ?? 2_000));
    child.stdin.end();
    try { await ended; } finally { clearTimeout(terminate); clearTimeout(kill); }
  })();
  try { await new Promise<void>((resolve, reject) => {
    const timeout = setTimeout(() => { child.kill(); reject(new Error('Desktop installation lease timed out')); }, 15_000);
    let output = '';
    let settled = false;
    const finish = (error?: Error) => { if (settled) return; settled = true; clearTimeout(timeout); error ? reject(error) : resolve(); };
    child.stdout.on('data', chunk => {
      output += chunk;
      if (output.includes('\n')) {
        try { if (JSON.parse(output.split('\n')[0]).ready === true) finish(); else finish(new Error('Invalid desktop lease receipt')); }
        catch { finish(new Error('Invalid desktop lease receipt')); }
      }
    });
    child.on('error', finish);
    child.once('close', () => finish(new Error(`Desktop installation lease unavailable: ${diagnostics}`)));
  }); } catch (error) { await close(); throw error; }
  child.once('close', () => { if (!closing) options.onLost?.(new Error(`Desktop installation lease lost after child exit: ${diagnostics}`)); });
  return { close };
}
