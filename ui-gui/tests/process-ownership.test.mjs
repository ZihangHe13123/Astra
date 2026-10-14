import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { EventEmitter, once } from 'node:events';
import test from 'node:test';
import { identifyLiveWindowsRoot, identifySpawnedProcess, probeChildHandle, processBirth, selectOwnedProcesses } from './process-ownership.mjs';

const processRow = (pid, ppid, born, name = 'fixture') => ({ pid, ppid, born, name });
const root = processRow(6036, 3904, '2026-10-14T11:18:14.6109590Z', 'node.exe');
const child = processRow(3568, 6036, '2026-10-14T11:18:16.9381040Z', 'python.exe');
const ids = value => value.owned.map(row => row.pid).sort((a, b) => a - b);

test('ordinary descendants are followed independent of snapshot row order', () => {
  const grandchild = processRow(5072, child.pid, '2026-10-14T11:18:16.9702700Z');
  const result = selectOwnedProcesses([grandchild, child, root], root);
  assert.deepEqual(ids(result), [3568, 5072, 6036]);
  assert.deepEqual(result.unresolved, []);
});

test('actual Page09 stale Windows parent IDs do not own earlier system processes', () => {
  const csrss = processRow(1648, 3568, '2026-10-14T11:04:19.8535720Z', 'csrss.exe');
  const winlogon = processRow(3528, 3568, '2026-10-14T11:04:19.8740340Z', 'winlogon.exe');
  const font = processRow(6864, 3528, '2026-10-14T11:04:19.9365890Z', 'fontdrvhost.exe');
  const dwm = processRow(6948, 3528, '2026-10-14T11:04:19.9561880Z', 'dwm.exe');
  const result = selectOwnedProcesses([root, child, csrss, winlogon, font, dwm], root);
  assert.deepEqual(ids(result), [3568, 6036]);
  assert.deepEqual(result.unresolved, []);
});

test('known orphan keeps its exact identity after its parent disappears', () => {
  const known = new Map([[child.pid, child]]);
  const orphan = { ...child, ppid: 1 };
  const descendant = processRow(5072, orphan.pid, '2026-10-14T11:18:16.9702700Z');
  assert.deepEqual(ids(selectOwnedProcesses([descendant, orphan], root, known)), [3568, 5072]);
});

test('reused root or known-child PID cannot acquire a new process tree', () => {
  const replacement = { ...child, born: '2026-10-14T11:20:00.0000000Z', ppid: 1 };
  const descendant = processRow(5072, child.pid, '2026-10-14T11:20:00.1000000Z');
  assert.deepEqual(ids(selectOwnedProcesses([replacement, descendant], root, new Map([[child.pid, child]]))), []);
  const replacementRoot = { ...root, born: '2026-10-14T11:20:00.0000000Z' };
  assert.deepEqual(ids(selectOwnedProcesses([replacementRoot, child], root)), []);
});

test('unparseable related timestamps and duplicate identities fail closed', () => {
  const bad = { ...child, born: 'unknown' };
  const result = selectOwnedProcesses([root, bad], root);
  assert.deepEqual(ids(result), [6036]);
  assert.equal(result.unresolved.length, 1);
  assert.equal(selectOwnedProcesses([bad], root, new Map([[child.pid, child]])).unresolved.length, 1);
  assert.equal(selectOwnedProcesses([root, root], root).unresolved.length, 1);
  assert.equal(selectOwnedProcesses([child], null).unresolved.length, 1);
  assert.equal(processBirth('2026-02-31T11:18:16Z'), null);
});

test('creation ordering preserves CIM sub-millisecond precision', () => {
  const parent = processRow(1, 0, '2026-10-14T11:18:16.9381040Z');
  const old = processRow(2, 1, '2026-10-14T11:18:16.9381030Z');
  const current = processRow(3, 1, '2026-10-14T11:18:16.9381050Z');
  assert.deepEqual(ids(selectOwnedProcesses([parent, old, current], parent)), [1, 3]);
});

test('initial root identity is bounded to this spawn, with POSIX timestamp precision', () => {
  const before = Date.parse('2026-10-14T11:18:14.610Z'), after = before + 2;
  assert.equal(identifySpawnedProcess([root], root.pid, before, after), root);
  assert.equal(identifySpawnedProcess([{ ...root, born: '2026-10-14T11:20:00Z' }], root.pid, before, after), null);
  assert.equal(identifySpawnedProcess([child], root.pid, before, after), null);
  const posix = { ...root, born: 'Wed Oct 14 11:18:14 2026' };
  const local = Date.parse(posix.born);
  assert.equal(identifySpawnedProcess([posix], posix.pid, local + 600, local + 650), posix);
});

test('Windows original handle proof binds a unique identity without comparing wall clocks', () => {
  assert.equal(identifySpawnedProcess([root], root.pid, 0, 1), null);
  assert.equal(identifyLiveWindowsRoot([root], root.pid, true), root);
  const replacement = { ...root, born: '2026-10-14T11:20:00.0000000Z' };
  assert.equal(identifyLiveWindowsRoot([replacement], root.pid, false), null);
  assert.equal(identifyLiveWindowsRoot([root], root.pid, undefined), null);
  assert.equal(identifyLiveWindowsRoot([], root.pid, true), null);
  assert.equal(identifyLiveWindowsRoot([root, root], root.pid, true), null);
  assert.equal(identifyLiveWindowsRoot([{ ...root, born: null }], root.pid, true), null);
});

test('a live child survives signal-zero probing and still receives explicit cleanup', { timeout: 10000 }, async () => {
  const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { stdio: 'ignore' });
  const exited = once(child, 'exit');
  await once(child, 'spawn');
  try {
    assert.deepEqual(probeChildHandle(child), { alive: true, error: null });
    // Node marks killed even for signal zero; it is not exit evidence.
    assert.equal(child.killed, true);
    assert.equal(child.exitCode, null);
    assert.equal(child.signalCode, null);
  } finally {
    child.kill('SIGKILL');
    await exited;
  }
  assert.deepEqual(probeChildHandle(child), { alive: false, error: null });
});

test('signal-zero errors reject identity proof without pretending the child exited', () => {
  const child = new EventEmitter();
  child.pid = 1234; child.exitCode = null; child.signalCode = null;
  const signals = [];
  child.kill = signal => {
    signals.push(signal);
    child.emit('error', Object.assign(new Error('controlled probe denial'), { code: 'EPERM' }));
    return false;
  };
  assert.deepEqual(probeChildHandle(child), { alive: false, error: 'EPERM' });
  assert.deepEqual(signals, [0]);
  assert.equal(child.exitCode, null);
  assert.equal(child.signalCode, null);
  assert.equal(child.listenerCount('error'), 0);
  child.kill = () => { throw Object.assign(new Error('controlled invalid handle'), { code: 'EINVAL' }); };
  assert.deepEqual(probeChildHandle(child), { alive: false, error: 'EINVAL' });
  assert.equal(child.listenerCount('error'), 0);
});

test('an exited Windows child with code 259 is not live', { skip: process.platform !== 'win32', timeout: 10000 }, async () => {
  const child = spawn(process.execPath, ['-e', 'process.exit(259)'], { stdio: 'ignore' });
  const [code] = await once(child, 'exit');
  assert.equal(code, 259);
  assert.deepEqual(probeChildHandle(child), { alive: false, error: null });
});
