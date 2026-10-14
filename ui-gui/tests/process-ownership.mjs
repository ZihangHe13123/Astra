// Diagnostic process ownership must use creation identities, not a PPID alone:
// Windows retains an exited parent's PID after that PID has been reused.
export function processBirth(born) {
  if (typeof born !== 'string') return null;
  const iso = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z$/.exec(born);
  if (iso) {
    const milliseconds = Date.parse(`${iso[1]}Z`);
    if (!Number.isFinite(milliseconds) || new Date(milliseconds).toISOString().slice(0, 19) !== iso[1]) return null;
    return BigInt(milliseconds) * 1000000n + BigInt((iso[2] || '').padEnd(9, '0'));
  }
  if (!/^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) [A-Z][a-z]{2} [ \d]\d \d{2}:\d{2}:\d{2} \d{4}$/.test(born)) return null;
  const milliseconds = Date.parse(born);
  return Number.isFinite(milliseconds) ? BigInt(milliseconds) * 1000000n : null;
}

export function identifySpawnedProcess(rows, pid, before, after) {
  const matches = rows.filter(row => row.pid === pid);
  if (matches.length !== 1) return null;
  const row = matches[0], born = processBirth(row.born);
  if (born === null) return null;
  // POSIX ps lstart has one-second precision; Windows CIM preserves fractions.
  const secondResolution = !/^\d{4}-/.test(row.born);
  const earliest = BigInt(secondResolution ? Math.floor(before / 1000) * 1000 : before) * 1000000n;
  const latest = BigInt(after + 1) * 1000000n;
  return born >= earliest && born < latest ? row : null;
}

export function probeChildHandle(child) {
  let emittedError;
  const onError = error => { emittedError = error; };
  child.on('error', onError);
  try {
    // On Windows libuv checks the original spawn handle, including a zero-time
    // wait for exit. Do not replace this with process.kill(pid, 0).
    const alive = child.kill(0) === true;
    return { alive: alive && !emittedError, error: emittedError ? String(emittedError.code || emittedError.name || 'Error') : null };
  } catch (error) {
    return { alive: false, error: String(error.code || error.name || 'Error') };
  } finally {
    child.removeListener('error', onError);
  }
}

export function identifyLiveWindowsRoot(rows, pid, handleAlive) {
  if (handleAlive !== true || !Number.isInteger(pid) || pid <= 0) return null;
  const matches = rows.filter(row => row.pid === pid);
  if (matches.length !== 1 || processBirth(matches[0].born) === null) return null;
  // Called only after the snapshot and a successful probe of the original
  // child handle. A still-live child cannot have had its PID reused meanwhile.
  return matches[0];
}

export function selectOwnedProcesses(rows, root, known = new Map()) {
  const owned = new Map(), unresolved = [], byPid = new Map();
  if (!root || processBirth(root.born) === null) return { owned: [], unresolved: ['Fixture root creation identity is unavailable'] };
  for (const row of rows) {
    if (byPid.has(row.pid)) return { owned: [], unresolved: [`Duplicate PID ${row.pid} in process snapshot`] };
    byPid.set(row.pid, row);
  }
  // Exact identities retain known descendants even after their parent exits.
  for (const row of rows) {
    const previous = row.pid === root.pid ? root : known.get(row.pid);
    if (!previous) continue;
    if (processBirth(row.born) === null) unresolved.push(`Cannot verify creation identity for PID ${row.pid}`);
    else if (row.born === previous.born) owned.set(row.pid, row);
  }
  for (let changed = true; changed;) {
    changed = false;
    for (const row of rows) {
      if (owned.has(row.pid)) continue;
      const parent = owned.get(row.ppid);
      if (!parent) continue;
      const born = processBirth(row.born), parentBorn = processBirth(parent.born);
      if (!Number.isInteger(row.pid) || row.pid <= 0 || born === null || parentBorn === null) {
        unresolved.push(`Cannot verify parent/child creation order for PID ${row.pid}`);
        continue;
      }
      // An older process belongs to a previous occupant of its recorded PPID.
      if (born < parentBorn) continue;
      owned.set(row.pid, row); changed = true;
    }
  }
  return { owned: [...owned.values()], unresolved: [...new Set(unresolved)] };
}
