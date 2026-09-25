import { test } from "node:test";
import assert from "node:assert/strict";
import { initialSession, projectEvent } from "../src/session-state.js";
import { createBackendEventReceiver } from "../src/backend-protocol.js";

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
