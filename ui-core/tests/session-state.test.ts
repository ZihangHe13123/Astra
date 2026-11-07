import { test } from "node:test";
import assert from "node:assert/strict";
import { initialSession, projectEvent } from "../src/session-state.js";
import { createBackendEventReceiver, EventDeliveryState } from "../src/backend-protocol.js";

test("partial output survives an error; history replaces instead of appending", () => {
  let state = initialSession("a");
  state = projectEvent(state, { type: "chunk", content: "你好" });
  state = projectEvent(state, { type: "error", message: "连接中断", partial: true });
  assert.deepEqual(state.messages.map(m => m.content), ["你好", "连接中断"]);
  state = projectEvent(state, { type: "history", session_id: "s", messages: [{ role: "user", content: "早上好" }, { role: "assistant", content: "你好" }] });
  state = projectEvent(state, { type: "history", session_id: "s", messages: [{ role: "user", content: "早上好" }, { role: "assistant", content: "你好" }] });
  assert.equal(state.messages.length, 2);
  assert.equal(state.busy, false);
});
test("duplicate events do not duplicate text or revive answered controls", () => {
  let state = initialSession("a");
  const receive = createBackendEventReceiver(e => { state = projectEvent(state, e); }, () => assert.fail());
  receive(JSON.stringify({ type: "chunk", event_id: "1", content: "one" }));
  receive(JSON.stringify({ type: "chunk", event_id: "1", content: "one" }));
  receive(JSON.stringify({ type: "tool_approval_request", event_id: "2", request_id: "approval" }));
  receive(JSON.stringify({ type: "approval_resolved", event_id: "3", request_id: "approval" }));
  receive(JSON.stringify({ type: "tool_approval_request", event_id: "2", request_id: "approval" }));
  assert.equal(state.messages[0].content, "one");
  assert.equal(state.approvals.length, 0);
});
test("tools and background workers do not keep the reply running after done", () => {
  let state = projectEvent(initialSession("a"), { type: "tool_calls", calls: [{ id: "c", name: "read_file" }] });
  state = projectEvent(state, { type: "tool_result", call_id: "c", output: "x", error: "" });
  state = projectEvent(state, { type: "agent_team", team_id: "t", agent_id: "w", event: "team_agent_idle" });
  state = projectEvent(state, { type: "done" });
  assert.equal(state.busy, false);
  assert.equal(state.tools[0].status, "completed");
  assert.equal(Object.keys(state.teams).length, 1);
});
test("side-command completion cannot end an active task", () => {
  let state = projectEvent(initialSession('a'), {type:'task_started', task:{id:'t',status:'running'}});
  state = projectEvent(state, {type:'done'});
  assert.equal(state.busy,true);
  state = projectEvent(state, {type:'task_status',task:{id:'t',status:'completed'}});
  assert.equal(state.busy,false);
});
test('rejected controls retire stale approvals but retain a correctable question', () => {
  let state = projectEvent(initialSession('a'), {type:'tool_approval_request',request_id:'p'});
  state = projectEvent(state, {type:'approval_response_rejected',request_id:'p',reason:'Expired'});
  assert.equal(state.approvals.length,0);
  assert.equal(state.messages.at(-1)?.content,'Expired');
  state = projectEvent(state, {type:'user_question_request',request_id:'q',questions:[]});
  state = projectEvent(state, {type:'user_question_response_rejected',request_id:'q',retryable:true,reason:'Choose one'});
  assert.equal(state.questions.length,1);
  state = projectEvent(state, {type:'user_question_response_rejected',request_id:'q',retryable:false,reason:'Expired'});
  assert.equal(state.questions.length,0);
});
test('team task updates preserve member state and team identity', () => {
  let state = projectEvent(initialSession('a'), {type:'agent_team',event:'team_created',team_id:'t',name:'Research',status:'active'});
  state = projectEvent(state, {type:'agent_team',event:'team_agent_created',team_id:'t',agent_id:'w',name:'Worker',status:'running'});
  state = projectEvent(state, {type:'agent_team',event:'team_task_updated',team_id:'t',team_task_id:'job',title:'Check',status:'completed'});
  state = projectEvent(state, {type:'agent_team',event:'team_agent_idle',team_id:'t',agent_id:'w',status:'idle'});
  assert.equal(state.teams.t.name,'Research'); assert.equal(state.teams.t.status,'active');
  assert.equal(state.teams.t.tasks[0].status,'completed'); assert.equal(state.teams.t.agents[0].status,'idle');
  state = projectEvent(state, {type:'agent_team',event:'team_stopped',team_id:'t',status:'stopped'});
  assert.equal(state.teams.t.agents[0].status,'cancelled');
});

test('setup and transport notices keep a provisional session blank until a real turn',()=>{
 let state={...initialSession('draft'),isDraft:true};
 state=projectEvent(state,{type:'gui_appshot',notice:'broker_unavailable'});
 state=projectEvent(state,{type:'gui_model_result',error:'Missing credentials'});
 state=projectEvent(state,{type:'history',messages:[],session_id:'new'});
 assert.equal(state.isDraft,true); assert.equal(state.messages.length,0);
 state=projectEvent(state,{type:'gui_user',text:'hello',submission_id:'send'});
 assert.equal(state.isDraft,false); assert.equal(state.messages.length,1);
});

test('entering an isolated namespace retires the generic provisional identity',()=>{
 const state=projectEvent({...initialSession('draft'),isDraft:true},{type:'mode_info',mode:'local',local_session:'fixture'});
 assert.equal(state.isDraft,false);assert.equal(state.session,'fixture');
});

test('known rejection differs from uncertain delivery and settles the pending message', () => {
 let state = projectEvent(initialSession('a'), {type:'gui_user',submission_id:'s',text:'hello'});
 assert.equal(state.messages[0].submissionState,'pending');
 state = projectEvent(state,{type:'submission_status',submission_id:'s',status:'unknown'});
 assert.equal(state.messages[0].submissionState,'unknown');assert.equal(state.messages[0].pending,true);
 state = projectEvent(state,{type:'message_rejected',submission_id:'s',reason:'Backend is busy'});
 assert.equal(state.messages[0].submissionState,'rejected');assert.equal(state.messages[0].pending,false);
 assert.equal(state.messages[0].submissionError,'Backend is busy');
 state = projectEvent(state,{type:'submission_status',submission_id:'s',status:'unknown'});
 assert.equal(state.messages[0].submissionState,'rejected');
 assert.equal(state.messages[0].content,'hello');
});

test('disconnect marks unacknowledged delivery unknown while late acceptance settles it', () => {
 let state = projectEvent(initialSession('a'),{type:'gui_user',submission_id:'s',text:'hello'});
 state = projectEvent(state,{type:'gui_disconnected'});
 assert.equal(state.messages[0].submissionState,'unknown');assert.equal(state.messages[0].pending,true);
 state = projectEvent(state,{type:'message_accepted',submission_id:'s'});
 state = projectEvent(state,{type:'submission_status',submission_id:'s',status:'unknown'});
 assert.equal(state.messages[0].submissionState,'accepted');assert.equal(state.messages[0].pending,false);
});

test('command results without call IDs receive unique numbers while repeated tool receipts retain theirs', () => {
 let state = initialSession('a');
 for (const name of ['yolo','model']) state = projectEvent(state,{type:'tool_result',name,output:'ok'});
 state = projectEvent(state,{type:'tool_calls',calls:[{id:'c',name:'read_file'}]});
 state = projectEvent(state,{type:'tool_result',call_id:'c',output:'done'});
 state = projectEvent(state,{type:'tool_result',name:'mode',output:'ok'});
 state = projectEvent(state,{type:'tool_result',call_id:'c',output:'done'});
 assert.deepEqual(state.tools.map(t=>t.result_index),[1,2,3,4]);
});

test('connection lifecycle survives panel remounts and rejects late events from prior logins', () => {
 let state = projectEvent(initialSession('a'),{type:'connection_pending',request_id:'old',route_id:'codex'});
 state = projectEvent(state,{type:'connection_auth',request_id:'old',user_code:'OLD'});
 state = projectEvent(state,{type:'connection_pending',request_id:'new',route_id:'codex'});
 state = projectEvent(state,{type:'connection_result',request_id:'old',error:'Cancelled'});
 state = projectEvent(state,{type:'connection_auth',request_id:'old',user_code:'OLD'});
 assert.equal(state.info.connection_pending.request_id,'new');
 assert.equal(state.info.connection_auth,undefined);assert.equal(state.info.connection_result,undefined);
 state = projectEvent(state,{type:'connection_auth',request_id:'new',user_code:'NEW'});
 assert.equal(state.info.connection_auth.user_code,'NEW');
 state = projectEvent(state,{type:'connection_result',request_id:'new',notice:'Signed in'});
 state = projectEvent(state,{type:'connection_auth',request_id:'new',user_code:'EXPIRED'});
 assert.equal(state.info.connection_pending.status,'completed');
 assert.equal(state.info.connection_auth.user_code,'NEW');
});

test('disconnect terminates a pending connection so reopened settings can retry', () => {
 let state = projectEvent(initialSession('a'),{type:'connection_pending',request_id:'login',route_id:'codex'});
 state = projectEvent(state,{type:'gui_disconnected'});
 assert.equal(state.info.connection_pending.status,'completed');
 assert.equal(state.info.connection_result.request_id,'login');
 assert.match(state.info.connection_result.error,/中断/);
});
test("another Astra session's message is a notice, never the user's words", () => {
  let state = projectEvent(initialSession("a"), { type: "peer_message", direction: "in", peer: "跑测试", task_id: "mac:t-1", state: "completed", text: "2 failures" });
  state = projectEvent(state, { type: "peer_message", direction: "out", peer: "跑测试", task_id: "mac:t-2", state: "submitted", text: "Run lint" });
  assert.deepEqual(state.messages.map(m => [m.role, m.content]), [
    ["system", "来自 跑测试（completed）：2 failures"], ["system", "发给 跑测试（submitted）：Run lint"]]);
  state = projectEvent(state, { type: "history", session_id: "s", messages: [{ role: "system", content: "PEER ← 跑测试 · submitted · mac:t-3\nHi" }] });
  assert.equal(state.messages[0].role, "system");
});

test("saved memories and the ones a turn recalls are shown as notices", () => {
  let state = projectEvent(initialSession("a"), { type: "memory_notice", event: "saved", memory_id: "0123456789abcdef", summary: "Prefers file:line citations", kind: "preference", scope: "global" });
  state = projectEvent(state, { type: "memory_notice", event: "revised", memory_id: "fedcba9876543210", summary: "Parking moved to B2", kind: "user_fact", scope: "project", previous_id: "0123456789abcdef" });
  state = projectEvent(state, { type: "memory_recall", count: 4, items: ["a", "b", "c", "d"] });
  state = projectEvent(state, { type: "memory_notice", event: "proposed", count: 2, summary: "Prefers pnpm" });
  state = projectEvent(state, { type: "memory_notice", event: "saved", memory_id: "aaaabbbbccccdddd", summary: "Uses pnpm", kind: "preference", scope: "global", origin: "consolidation" });
  assert.deepEqual(state.messages.map(m => [m.role, m.content]), [
    ["system", "已记住：Prefers file:line citations（#01234567）"],
    ["system", "已更新记忆：Parking moved to B2（#fedcba98 · 本项目）"],
    ["system", "回忆了 4 条记忆：a；b；c"],
    ["system", "记忆整理：2 条建议待你审核（/memory proposals），如“Prefers pnpm”"],
    ["system", "记忆整理 · 已记住：Uses pnpm（#aaaabbbb）"]]);
  assert.equal(state.busy, false);
});

test("a turn started for ended background work is announced as a notice", () => {
  let state = projectEvent(initialSession("a"), { type: "background_wakeup", state: "started", message: "后台任务完成，继续处理：npm test" });
  state = projectEvent(state, { type: "background_wakeup", state: "limited", message: "结果会在你下一条消息时交给 Astra。" });
  assert.deepEqual(state.messages.map(m => [m.role, m.content]), [
    ["system", "后台任务完成，继续处理：npm test"], ["system", "结果会在你下一条消息时交给 Astra。"]]);
  assert.equal(state.busy, false);
});

test('timed questions stay answerable after completion and expire visibly on disconnect', () => {
  let state = projectEvent(initialSession('a'), {type:'user_question_request',request_id:'q',state:'waiting',mode:'timed',questions:[]});
  state = projectEvent(state, {type:'user_question_pending',request_id:'q',state:'pending'});
  state = projectEvent(state, {type:'done'});
  assert.equal(state.questions[0].state, 'pending');
  assert.equal(state.busy, false);
  state = projectEvent(state, {type:'gui_disconnected'});
  assert.equal(state.questions.length, 0);
  assert.match(state.messages.at(-1)?.content || '', /失效/);
});

test('expired question notices survive history replacement when recovered after restart', () => {
  let state = projectEvent(initialSession('a'), {type:'history',messages:[],session_id:'s'});
  state = projectEvent(state, {type:'user_question_resolved',request_id:'old',state:'expired',reason:'Question expired after backend restart.'});
  assert.match(state.messages.at(-1)?.content || '', /expired after backend restart/);
});
test("tool calls and a turn's file changes remember where in the conversation they happened", () => {
  let state = initialSession("a");
  state = projectEvent(state, { type: "gui_user", text: "改一下", submission_id: "u1" });
  state = projectEvent(state, { type: "tool_calls", calls: [{ id: "c1", name: "read_file" }, { id: "c2", name: "edit_file" }] });
  state = projectEvent(state, { type: "chunk", content: "好了。" });
  const answer = state.messages[state.messages.length - 1].id;
  state = projectEvent(state, { type: "tool_calls", calls: [{ id: "c3", name: "execute_shell" }] });
  state = projectEvent(state, { type: "tool_result", call_id: "c3", output: "ok", error: "" });
  assert.deepEqual(state.tools.map(t => [t.call_id, t.after_message]), [["c1", "u1"], ["c2", "u1"], ["c3", answer]]);
  assert.ok(state.tools.every(t => typeof t.started_at === "number"));
  // A result keeps the place its call had.
  assert.equal(state.tools[2].status, "completed");

  const report = { type: "turn_changes", session_id: "s", request_id: "r1", turn_seq: 1, files: [{ path: "a.py", state: "modified", added: 3, removed: 1, compare: "full", reason: "" }], unknown_count: 0, totals: { files: 1, added: 3, removed: 1 } };
  state = projectEvent(state, report);
  state = projectEvent(state, { type: "gui_user", text: "再改", submission_id: "u2" });
  // The same report delivered again stays where it was, once.
  state = projectEvent(state, report);
  state = projectEvent(state, { ...report, request_id: "r2", turn_seq: 2 });
  assert.deepEqual(state.turnChanges.map(note => [note.request_id, note.after_message]), [["r1", answer], ["r2", "u2"]]);
  assert.equal(state.info.turn_changes.request_id, "r2");

  // A restored history has no places to give: its tool results are listed, its turn reports are not.
  state = projectEvent(state, { type: "history", session_id: "s", messages: [{ role: "user", content: "改一下" }], tool_results: [{ name: "read_file", output: "x", error: "" }] });
  assert.deepEqual(state.turnChanges, []);
  assert.equal(state.tools[0].after_message, undefined);
});

test("a job's result joins the personal thread as Astra's message, the way history keeps it", () => {
  let state = projectEvent(initialSession("a"), { type: "chunk", content: "好的" });
  state = projectEvent(state, { type: "done" });
  state = projectEvent(state, { type: "job_result", title: "定时任务：AI 晚报", text: "三条新闻", job_id: "j", run_id: "r", state: "completed" });
  assert.deepEqual(state.messages.map(m => [m.role, m.content]), [["assistant", "好的"], ["assistant", "【定时任务：AI 晚报】\n三条新闻"]]);
  assert.equal(state.busy, false);
  // Reloaded, the thread shows the same words: the backend stored them as an assistant message.
  const reloaded = projectEvent(state, { type: "history", session_id: "personal", messages: [{ role: "assistant", content: "【定时任务：AI 晚报】\n三条新闻" }] });
  assert.equal(reloaded.messages.at(-1)?.content, state.messages.at(-1)?.content);
});

// One backend connection as the clients receive it: live frames carry a sequence, and a skipped
// sequence makes the receiver ask for the event_stream.replay page of journaled events.
function connection() {
  let state = projectEvent(initialSession("a"), { type: "gui_ready" });
  const gaps: number[] = [];
  const handled: string[] = [];
  const receive = createBackendEventReceiver(event => {
    handled.push(`${event.type}:${(event as { request_id?: string }).request_id ?? ""}`);
    state = projectEvent(state, event);
  }, () => assert.fail(), { delivery: new EventDeliveryState(), onGap: cursor => gaps.push(cursor) });
  let sequence = 0;
  const live = (cursor: number, event: object, { lost = false, replayable = true } = {}) => {
    sequence++;
    if (!lost) receive(JSON.stringify({ ...event, generation: "g", sequence, cursor, replayable, event_id: `E${sequence}` }));
  };
  const replay = (cursor: number, sequence: number, event: object) =>
    receive(JSON.stringify({ ...event, cursor, sequence: cursor, replayable: true, replayed: true, event_id: `E${sequence}` }));
  return { live, replay, gaps, handled, receive, get state() { return state; } };
}

test("a lost approval request replayed after its live resolution stays resolved", () => {
  const c = connection();
  const request = { type: "tool_approval_request", request_id: "r1", tool_name: "bash", choices: ["once", "deny"] };
  c.live(9, { type: "task_started", task: { id: "t1", status: "running" } });
  c.live(10, request, { lost: true });
  c.live(11, { type: "approval_resolved", request_id: "r1", decision: "once" });
  assert.deepEqual(c.gaps, [9]);
  c.replay(10, 2, request);
  c.replay(11, 3, { type: "approval_resolved", request_id: "r1", decision: "once" });
  c.receive(JSON.stringify({ type: "event_replay_complete", after_cursor: 9, next_cursor: 11, has_more: false }));
  assert.deepEqual(c.state.approvals, []);
  // No client is handed the stale request; the terminal client keeps its own list from these events.
  assert.equal(c.handled.includes("tool_approval_request:r1"), false);
  c.live(12, { type: "task_status", task: { id: "t1", status: "completed" } });
  c.live(13, { type: "done" });
  assert.deepEqual(c.state.approvals, []);
  assert.equal(c.state.busy, false);
});

test("a finished task's own approvals and waiting questions end with it; a worker's approval and a pending question remain", () => {
  const c = connection();
  c.live(8, { type: "task_started", task: { id: "t1", status: "running" } });
  c.live(9, { type: "tool_calls", calls: [{ id: "call-1", name: "bash" }] });
  c.live(10, { type: "tool_approval_request", request_id: "r1", call_id: "call-1", tool_name: "bash" });
  // A subagent asks for a call of its own, which is not in the conversation's tool list.
  c.live(11, { type: "tool_approval_request", request_id: "r2", call_id: "worker-call", tool_name: "bash" });
  c.live(11, { type: "user_question_request", request_id: "q1", task_id: "t1", state: "waiting", questions: [] }, { replayable: false });
  c.live(11, { type: "user_question_request", request_id: "q2", task_id: "t1", state: "waiting", mode: "timed", questions: [] }, { replayable: false });
  c.live(11, { type: "user_question_pending", request_id: "q2", task_id: "t1", state: "pending" }, { replayable: false });
  // r1's resolution is lost and the turn's end arrives first, so the replayed resolution is superseded.
  c.live(12, { type: "approval_resolved", request_id: "r1", decision: "deny" }, { lost: true });
  c.live(13, { type: "task_status", task: { id: "t1", status: "cancelled" } });
  c.live(14, { type: "done" });
  assert.deepEqual(c.gaps, [11]);
  c.replay(12, 8, { type: "approval_resolved", request_id: "r1", decision: "deny" });
  c.receive(JSON.stringify({ type: "event_replay_complete", after_cursor: 11, next_cursor: 12, has_more: false }));
  assert.deepEqual(c.state.approvals.map(a => a.request_id), ["r2"]);
  assert.deepEqual(c.state.questions.map(q => q.request_id), ["q2"]);
  assert.equal(c.state.busy, false);
});

test("a finished background run stays finished when its older running event is replayed", () => {
  const c = connection();
  const run = (status: string, event: string) =>
    ({ type: "process_status", event, process_id: "p1", kind: "shell", label: "npm test", status, started_at: 1 });
  c.live(8, { type: "task_started", task: { id: "t1", status: "running" } });
  c.live(9, run("running", "process_started"), { lost: true });
  c.live(10, run("running", "process_output"), { lost: true });
  c.live(11, run("completed", "process_completed"));
  assert.deepEqual(c.gaps, [8]);
  c.replay(9, 2, run("running", "process_started"));
  c.replay(10, 3, run("running", "process_output"));
  c.replay(11, 4, run("completed", "process_completed"));
  c.receive(JSON.stringify({ type: "event_replay_complete", after_cursor: 8, next_cursor: 11, has_more: false }));
  assert.equal(c.state.processes.p1.status, "completed");
  // Neither client is handed the older events, so the terminal client's own list stays right too.
  assert.equal(c.handled.filter(event => event.startsWith("process_status")).length, 1);
});

test("an ended task is not restarted by its replayed start, while a later resume of the same task is", () => {
  const c = connection();
  c.live(7, { type: "gui_ready" }, { replayable: false });
  c.live(8, { type: "task_started", task: { id: "t1", status: "running" } }, { lost: true });
  c.live(9, { type: "task_status", task: { id: "t1", status: "interrupted" } });
  assert.deepEqual(c.gaps, [0]);
  c.replay(8, 2, { type: "task_started", task: { id: "t1", status: "running" } });
  c.replay(9, 3, { type: "task_status", task: { id: "t1", status: "interrupted" } });
  c.receive(JSON.stringify({ type: "event_replay_complete", after_cursor: 0, next_cursor: 9, has_more: false }));
  assert.equal(c.state.info.task_status.task.status, "interrupted");
  assert.equal(c.state.busy, false);
  // /resume continues the same task id later in the journal: that start is newer and applies.
  c.live(10, { type: "task_started", task: { id: "t1", status: "running" } });
  assert.equal(c.state.busy, true);
});

test("an earlier task's replayed end closes its own approval but not the turn that started after it", () => {
  const c = connection();
  c.live(8, { type: "task_started", task: { id: "A", status: "running" } });
  c.live(9, { type: "tool_calls", calls: [{ id: "call-a", name: "bash" }] });
  c.live(10, { type: "tool_approval_request", request_id: "ra", call_id: "call-a", tool_name: "bash" });
  c.live(11, { type: "task_status", task: { id: "A", status: "cancelled" } }, { lost: true });
  c.live(12, { type: "task_started", task: { id: "B", status: "running" } });
  assert.deepEqual(c.gaps, [10]);
  c.replay(11, 4, { type: "task_status", task: { id: "A", status: "cancelled" } });
  c.replay(12, 5, { type: "task_started", task: { id: "B", status: "running" } });
  c.receive(JSON.stringify({ type: "event_replay_complete", after_cursor: 10, next_cursor: 12, has_more: false }));
  assert.equal(c.state.info.task_status.task.id, "B");
  assert.equal(c.state.busy, true);
  assert.deepEqual(c.state.approvals, [], "A's own record still ends");
  c.live(13, { type: "task_status", task: { id: "B", status: "completed" } });
  assert.equal(c.state.busy, false);
});

test("an earlier task's start and end replayed after the next task began leave that task current", () => {
  const c = connection();
  c.live(7, { type: "done" });
  c.live(8, { type: "task_started", task: { id: "A", status: "running" } }, { lost: true });
  c.live(9, { type: "task_status", task: { id: "A", status: "completed" } }, { lost: true });
  c.live(10, { type: "task_started", task: { id: "B", status: "running" } });
  c.replay(8, 2, { type: "task_started", task: { id: "A", status: "running" } });
  c.replay(9, 3, { type: "task_status", task: { id: "A", status: "completed" } });
  c.receive(JSON.stringify({ type: "event_replay_complete", after_cursor: 7, next_cursor: 10, has_more: false }));
  assert.equal(c.state.info.task_status.task.id, "B");
  assert.equal(c.state.busy, true);
});
