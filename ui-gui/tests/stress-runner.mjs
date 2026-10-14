/** Bounded, diagnostic-only repetition of the unchanged desktop acceptance scripts. */
import { spawn, execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { appendFileSync, cpSync, existsSync, mkdirSync, readdirSync, renameSync, writeFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { identifyLiveWindowsRoot, identifySpawnedProcess, probeChildHandle, selectOwnedProcesses } from './process-ownership.mjs';

const exec = promisify(execFile);
const options = Object.fromEntries(process.argv.slice(2).map(value => value.split(/=(.*)/s).slice(0, 2)));
const root = resolve(options['--root'] || fileURLToPath(new URL('../..', import.meta.url)));
const suite = options['--suite'] || 'page';
const repeat = Number(options['--repeat'] || 1), timeout = Number(options['--timeout-seconds'] || 180) * 1000;
if (!['smoke', 'page'].includes(suite) || !Number.isInteger(repeat) || repeat < 1 || repeat > 20 || !Number.isFinite(timeout) || timeout < 1000 || timeout > 600000) throw new Error('Invalid bounded stress arguments');
const profileModes = (options['--profile-imports'] || 'on').split(',');
if (profileModes.some(mode => !['on', 'off'].includes(mode)) || ![1, repeat].includes(profileModes.length)) throw new Error('Import profiling must be on/off or one on/off value per iteration');
const profileSequence = Array.from({ length: repeat }, (_, index) => (profileModes.length === 1 ? profileModes[0] : profileModes[index]) === 'on');
const output = resolve(options['--output'] || join(root, 'output/desktop-stress', suite));
if (existsSync(output)) throw new Error(`Use a fresh stress output directory: ${output}`);
mkdirSync(output, { recursive: true });
const fixtureOutput = join(root, 'output/playwright', suite === 'smoke' ? 'gui' : 'page');
if (existsSync(fixtureOutput)) renameSync(fixtureOutput, join(output, 'preexisting-output'));
const run = { suite, repeat, timeoutMs: timeout, platform: process.platform, node: process.version,
  instrumentation: { debug: 'pw:browser', pythonProfileImportTime: profileModes.length === 1 ? profileSequence[0] : 'per-iteration',
    pythonProfileImportTimeSequence: profileSequence, processSamplingIntervalMs: 2000,
    interpretation: 'Import profiling and process sampling add overhead. A timeout in this mode alone does not establish a production timeout.' }, iterations: [] };
const save = () => writeFileSync(join(output, 'summary.json'), JSON.stringify(run, null, 2));
save();

async function snapshot() {
  if (process.platform === 'win32') {
    const script = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); @(Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -gt 0 } | ForEach-Object { [pscustomobject]@{pid=$_.ProcessId;ppid=$_.ParentProcessId;name=$_.Name;born=$(if ($_.CreationDate) { $_.CreationDate.ToUniversalTime().ToString('o') } else { $null });threads=$_.ThreadCount;handles=$_.HandleCount;workingSet=$_.WorkingSetSize;kernelTime=$_.KernelModeTime;userTime=$_.UserModeTime} }) | ConvertTo-Json -Compress";
    const { stdout } = await exec('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', script], { timeout: 5000, encoding: 'utf8', maxBuffer: 8 * 1024 * 1024, windowsHide: true });
    const rows = JSON.parse(stdout); return Array.isArray(rows) ? rows : [rows];
  }
  const { stdout } = await exec('ps', ['-axo', 'pid=,ppid=,lstart=,comm='], { timeout: 3000, encoding: 'utf8' });
  return stdout.split('\n').flatMap(line => {
    if (!line.trim()) return [];
    const match = /^\s*(\d+)\s+(\d+)\s+(.{24})\s+(.*)$/.exec(line);
    if (!match) throw new Error('Unparseable process snapshot row');
    return [{ pid: Number(match[1]), ppid: Number(match[2]), born: match[3], name: match[4] }];
  });
}

for (let index = 1; index <= repeat; index++) {
  const directory = join(output, String(index).padStart(2, '0')); mkdirSync(directory);
  const temporary = join(directory, 'tmp'); mkdirSync(temporary);
  const result = { index, pythonProfileImportTime: profileSequence[index - 1], startedAt: new Date().toISOString(), phases: [], exitCode: null, signal: null, timedOut: false, diagnosticErrors: [] };
  run.iterations.push(result); save();
  const phase = name => { result.phases.push({ name, at: new Date().toISOString() }); console.log(`STRESS ${suite} ${index}/${repeat} ${name}`); save(); };
  const known = new Map(); let child, rootIdentity, spawnCompletedAt, stopped = false, snapshotBusy = false;
  const capture = async () => {
    if (snapshotBusy) return [];
    snapshotBusy = true;
    try {
      const rows = await snapshot();
      const snapshotCompletedAt = Date.now();
      let rootBinding;
      if (!rootIdentity && child) {
        // Probe after the complete snapshot, before binding its root identity.
        const probe = process.platform === 'win32' && !stopped ? probeChildHandle(child) : null;
        const candidates = rows.filter(row => row.pid === child.pid).map(({ pid, ppid, born, name }) => ({ pid, ppid, born, name }));
        if (!stopped) rootIdentity = process.platform === 'win32'
          ? identifyLiveWindowsRoot(rows, child.pid, probe?.alive)
          : identifySpawnedProcess(rows, child.pid, spawnedAt, spawnCompletedAt);
        rootBinding = { method: process.platform === 'win32' ? 'spawn-handle-after-snapshot' : 'spawn-time-window',
          spawnedAt, spawnCompletedAt, snapshotCompletedAt, childPid: child.pid,
          stopped, probe, candidates, bound: Boolean(rootIdentity) };
      }
      const { owned, unresolved } = selectOwnedProcesses(rows, rootIdentity, known);
      appendFileSync(join(directory, 'processes.jsonl'), JSON.stringify({ at: new Date().toISOString(), owned, unresolved, rootBinding }) + '\n');
      for (const row of owned) known.set(row.pid, row);
      if (unresolved.length) {
        // A later empty snapshot cannot prove where an unidentified child went.
        result.ownershipUnverified = true;
        throw new Error(unresolved.join('; '));
      }
      return owned;
    } catch (error) { result.diagnosticErrors.push(String(error)); save(); return null; }
    finally { snapshotBusy = false; }
  };
  phase('spawn');
  const spawnedAt = Date.now();
  const env = { ...process.env, TMPDIR: temporary, TEMP: temporary, TMP: temporary,
    DEBUG: [process.env.DEBUG, 'pw:browser'].filter(Boolean).join(',') };
  // Any nonempty value enables CPython import profiling; off must be absent,
  // including when the runner itself inherited it from the calling shell.
  if (result.pythonProfileImportTime) env.PYTHONPROFILEIMPORTTIME = '1';
  else delete env.PYTHONPROFILEIMPORTTIME;
  child = spawn(process.execPath, [join(root, 'ui-gui/tests', suite === 'smoke' ? 'smoke.mjs' : 'page-smoke.mjs')], {
    cwd: root, env,
    windowsHide: true, stdio: ['ignore', 'pipe', 'pipe'],
  });
  spawnCompletedAt = Date.now();
  result.supervisorChildPid = child.pid;
  for (const [stream, name] of [[child.stdout, 'stdout.log'], [child.stderr, 'stderr.log']]) {
    stream.on('data', chunk => appendFileSync(join(directory, name), chunk));
  }
  const ended = new Promise(resolve => {
    child.on('error', error => {
      if (!child.pid) { result.spawnError = String(error); stopped = true; resolve(); }
      else {
        // A failed kill(0) probe is not a process-exit notification.
        result.ownershipUnverified = true; result.diagnosticErrors.push(String(error)); save();
      }
    });
    child.once('exit', (code, signal) => { result.exitCode = code; result.signal = signal; stopped = true; resolve(); });
  });
  await capture();
  const sampling = setInterval(() => { void capture(); }, 2000);
  let timer;
  await Promise.race([ended, new Promise(resolve => { timer = setTimeout(() => { result.timedOut = true; resolve(); }, Math.max(0, timeout - (Date.now() - spawnedAt))); })]);
  clearTimeout(timer); clearInterval(sampling); phase(result.timedOut ? 'deadline' : 'exited');
  // A completed wrapper is not proof its Electron/Python descendants stopped.
  while (snapshotBusy) await new Promise(resolve => setTimeout(resolve, 50));
  let remaining = await capture();
  if (!stopped || remaining?.length) {
    result.requiredCleanup = true; phase('cleanup');
    try {
      if (!stopped && child.pid) {
        // Use the spawned child handle; taskkill /T traverses unverified PPIDs.
        child.kill('SIGKILL');
      }
      // Revalidate recorded creation times; never stop a recycled PID.
      const descendants = (remaining || []).filter(row => row.pid !== child.pid);
      if (descendants.length && process.platform === 'win32') {
        const identities = descendants.map(row => `@{pid=${row.pid};born='${row.born.replaceAll("'", "''")}'}`).join(',');
        await exec('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', `$ErrorActionPreference='Stop'; $wanted=@(${identities}); $live=@(Get-CimInstance Win32_Process); foreach($item in $wanted) { $p=$live | Where-Object { $_.ProcessId -eq $item.pid -and $_.CreationDate.ToUniversalTime().ToString('o') -eq $item.born }; if($p) { Stop-Process -Id $item.pid -Force } }`], { timeout: 5000, windowsHide: true });
      } else if (descendants.length) {
        const rows = await snapshot();
        for (const row of descendants.reverse()) if (rows.some(item => item.pid === row.pid && item.born === row.born)) {
          try { process.kill(row.pid, 'SIGKILL'); } catch (error) { if (error.code !== 'ESRCH') throw error; }
        }
      }
    } catch (error) { result.cleanupError = String(error); }
    await Promise.race([ended, new Promise(resolve => setTimeout(resolve, 1000))]);
    remaining = await capture();
  }
  result.cleanupConfirmed = stopped && !result.ownershipUnverified && Array.isArray(remaining) && remaining.length === 0;
  child.stdout.destroy(); child.stderr.destroy();
  phase('preserve-evidence');
  if (existsSync(fixtureOutput)) renameSync(fixtureOutput, join(directory, 'playwright'));
  const logs = join(directory, 'backend-logs'); mkdirSync(logs);
  for (const fixture of readdirSync(temporary, { withFileTypes: true }).filter(entry => entry.isDirectory() && entry.name.startsWith('astra-'))) {
    const source = join(temporary, fixture.name, 'logs');
    if (existsSync(source)) for (const name of readdirSync(source).filter(name => /^gui-backend-.*\.log$/.test(name))) cpSync(join(source, name), join(logs, `${fixture.name}-${name}`));
  }
  result.finishedAt = new Date().toISOString();
  result.success = result.exitCode === 0 && !result.timedOut && !result.spawnError && !result.requiredCleanup && result.cleanupConfirmed;
  save();
  if (!result.cleanupConfirmed) { run.stoppedEarly = 'Fixture cleanup could not be confirmed; do not contaminate later repetitions.'; save(); break; }
}
run.success = run.iterations.length === repeat && run.iterations.every(iteration => iteration.success);
save(); console.log(JSON.stringify({ success: run.success, completed: run.iterations.length, requested: repeat, output }));
if (!run.success) process.exitCode = 1;
