import assert from 'node:assert/strict';
import { homedir } from 'node:os';
import { test } from 'node:test';
import { initialSession } from '@astra/ui-core/session-state';
import type { SessionEntry } from '../src/bridge.js';
import * as sidebar from '../src/renderer/sidebar.js';

const alpha = '/x/alpha';
const beta = '/x/beta';
const prefs = { titles: {}, pinned: [], projects: [alpha, beta] };
const saved = (name: string, modified: number, workspace?: string | null): SessionEntry => ({
  name, mode: 'work', modified, ...(workspace !== undefined ? { workspace } : {}),
});

test('P3 1 groups saved conversations by project and sorts each group by modification time', () => {
  const older = saved('older', 1, alpha);
  const newer = saved('newer', 4, alpha);
  const otherProject = saved('beta-session', 2, beta);
  const inHome = saved('home', 7, homedir());
  const unbound = saved('unbound', 6, null);
  const legacy = saved('legacy', 5);
  const groups = sidebar.sidebarGroups([older, otherProject, inHome, unbound, newer, legacy], [], prefs, '');

  assert.deepEqual(groups.projects, [
    { folder: alpha, name: 'alpha', entries: [newer, older] },
    { folder: beta, name: 'beta', entries: [otherProject] },
  ]);
  assert.deepEqual(groups.recent.flatMap(group => group.entries), [inHome, unbound, legacy]);
});

test('P3 2 keeps open and pinned shortcuts for conversations also listed under their project', () => {
  const pinned = saved('pinned', 1, alpha);
  const running = { ...initialSession('runtime', alpha), session: 'running', isDraft: false, status: 'ready' as const };
  const groups = sidebar.sidebarGroups([pinned], [running], { ...prefs, pinned: ['work:pinned'] }, '');
  const open = saved('running', 0, alpha);

  assert.deepEqual(groups.open, [open]);
  assert.deepEqual(groups.pinned, [pinned]);
  assert.deepEqual(groups.projects[0].entries, [pinned, open]);
  assert.deepEqual(groups.recent, []);
});

test('P3 3 matches project folders with trailing separators while preserving their display path', () => {
  const entry = saved('session', 1, alpha);
  const folder = `${alpha}/`;
  const groups = sidebar.sidebarGroups([entry], [], { ...prefs, projects: [folder] }, '');

  assert.deepEqual(groups.projects, [{ folder, name: 'alpha', entries: [entry] }]);
});

test('P3 4 finds conversations by project name and keeps empty projects only without a search', () => {
  const entry = saved('unrelated-name', 1, alpha);
  const preferences = { ...prefs, titles: { 'work:unrelated-name': 'An unrelated title' } };
  const search = sidebar.sidebarGroups([entry], [], preferences, 'ALPHA');

  assert.deepEqual(search.all, [entry]);
  assert.deepEqual(search.projects, [{ folder: alpha, name: 'alpha', entries: [entry] }]);
  assert.deepEqual(sidebar.sidebarGroups([entry], [], preferences, '').projects, [
    { folder: alpha, name: 'alpha', entries: [entry] },
    { folder: beta, name: 'beta', entries: [] },
  ]);
});

test('P3 5 labels conversations by their matching project folder', () => {
  assert.equal(sidebar.projectLabel(saved('a', 1, alpha), [`${alpha}/`, beta]), 'alpha');
  assert.equal(sidebar.projectLabel(saved('other', 1, '/x/other'), prefs.projects), '');
  assert.equal(sidebar.projectLabel(saved('legacy', 1, null), prefs.projects), '');
});

test('P3 6 retries only the first workspace mismatch for a different recorded folder', () => {
  const state = { session: 'session', mode: 'work', workspace: alpha };
  const event = { type: 'error', code: 'workspace_mismatch', workspace: beta };
  const retried = new Set<string>();
  assert.deepEqual(sidebar.mismatchRetry(event, state, retried), { ...state, workspace: beta });
  assert.equal(sidebar.mismatchRetry(event, state, new Set(['work:session'])), undefined);
  for (const rejected of [
    { ...event, workspace: alpha },
    { ...event, code: 'session_unavailable' },
    { ...event, type: 'tool_result' },
    { type: 'error', code: 'workspace_mismatch' },
    { ...event, workspace: '' },
  ]) {
    assert.equal(sidebar.mismatchRetry(rejected, state, retried), undefined);
  }
});
