import { execFile, spawn } from 'node:child_process';
import { join } from 'node:path';

export function runOfficeProcess(args: string[], directory: string, signal: AbortSignal, timeoutMs: number): Promise<string> {
  signal.throwIfAborted();
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, args, { cwd: directory, stdio: ['ignore','pipe','pipe'],
      env: {...process.env, ELECTRON_RUN_AS_NODE:'1', TMPDIR:directory, TEMP:directory, TMP:directory},
      windowsHide: true, detached: process.platform !== 'win32' });
    let stdout = '', stderr = '', failure: Error | undefined;
    const stop = (error: Error) => {
      if (failure) return;
      failure = error;
      if (!child.pid || child.exitCode !== null || child.signalCode !== null) return;
      if (process.platform === 'win32') {
        const taskkill = join(process.env.SystemRoot || 'C:\\Windows', 'System32','taskkill.exe');
        execFile(taskkill, ['/T','/F','/PID',String(child.pid)], {windowsHide:true,timeout:3000}, err => {
          if (err && child.exitCode === null && child.signalCode === null) child.kill('SIGKILL');
        });
      } else {
        try { process.kill(-child.pid, 'SIGKILL'); }
        catch (err) { if ((err as NodeJS.ErrnoException).code !== 'ESRCH') child.kill('SIGKILL'); }
      }
    };
    const abort = () => stop(new Error('文件预览已取消。'));
    const timeout = setTimeout(() => stop(Object.assign(new Error('文档转换超时。'), {code:'timeout'})), timeoutMs);
    signal.addEventListener('abort', abort, {once:true});
    const collect = (prior: string, chunk: string) => {
      if (prior.length + chunk.length <= 256 * 1024) return prior + chunk;
      stop(new Error('Office 转换输出过长。')); return prior;
    };
    child.stdout.setEncoding('utf8').on('data', (chunk: string) => {stdout = collect(stdout, chunk);});
    child.stderr.setEncoding('utf8').on('data', (chunk: string) => {stderr = collect(stderr, chunk);});
    child.once('error', error => {failure ??= error;});
    child.once('close', code => {
      clearTimeout(timeout);
      signal.removeEventListener('abort', abort);
      if (failure) reject(failure);
      else if (code === 0) resolve(stdout);
      else {
        try {
          const info = JSON.parse(stderr.trim());
          reject(Object.assign(new Error(String(info.error || 'Office 转换失败')), {code:info.code}));
        } catch { reject(new Error(`Office 转换进程已退出（${code ?? 'signal'}）。`)); }
      }
    });
  });
}
